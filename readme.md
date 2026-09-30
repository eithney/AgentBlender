# BlenderAI-OS

AI 原生的 3D 创作引擎第一步：Agent 进程与 Blender 进程通过本机 WebSocket 握手，并互发指令与场景事件。

通信只绑定 `127.0.0.1`。插件只执行白名单指令（`ping`、`get_scene`、`create_mesh`），不提供远程执行任意 Python 的入口。

## 目录

```text
protocol/                 两边共用的 JSON 消息
  messages.py
agent/
  server.py               通信中枢
  client.py               Agent 端；--role blender-sim 可在不打开 Blender 时自测
blender_addon/blenderai_os/
  __init__.py             事件监听、场景快照、指令执行
  bridge.py               WebSocket 客户端线程
```

## 1. Agent 侧环境

需要 Python 3.11+。在仓库根目录：

```bash
cd /c/dev/art/AgentBlender
python -m venv .venv
source .venv/Scripts/activate
python -m pip install -r requirements.txt
```

## 2. 启动通信中枢

```bash
python -m agent.server
```

默认地址：`ws://127.0.0.1:8765`。

## 3. 不打开 Blender 时先跑通握手

另开一个终端，同样激活 `.venv`：

```bash
python -m agent.client --role blender-sim
```

再开一个终端：

```bash
python -m agent.client --op ping
```

中枢日志出现 `bidirectional handshake complete`，Agent 端打印 `pong`，即表示双向链路已通。`get_scene` 也可以对模拟端发送。`create_mesh` 需要真实 Blender。

## 4. 把插件装进 Blender

插件运行在 Blender 自带的 Python 里，需要单独安装 `websockets`。在 Blender 的 Scripting 工作区打开 Python Console：

```python
import sys
print(sys.executable)
```

用打印出来的解释器安装依赖（路径按你的 Blender 版本替换）：

```bash
"/c/Program Files/Blender Foundation/Blender 4.5/4.5/python/bin/python.exe" -m ensurepip
"/c/Program Files/Blender Foundation/Blender 4.5/4.5/python/bin/python.exe" -m pip install "websockets>=12,<16"
```

用符号链接把插件指到仓库，这样改代码后刷新插件即可。把 `4.5` 换成你本机 `%APPDATA%/Blender Foundation/Blender/` 下的版本目录：

```bash
ADDONS="$APPDATA/Blender Foundation/Blender/4.5/scripts/addons"
mkdir -p "$ADDONS"
ln -sfn /c/dev/art/AgentBlender/blender_addon/blenderai_os "$ADDONS/blenderai_os"
```

然后在 Blender：Edit → Preferences → Add-ons，搜索 **BlenderAI-OS** 并启用。

3D 视口按 `N`，打开 **BlenderAI** 侧栏。状态变为「已连接中枢」后，保持 `python -m agent.server` 运行，在 Agent 终端执行：

```bash
python -m agent.client --op get_scene
python -m agent.client --op create_mesh --name Cup --primitive UV_SPHERE
python -m agent.client --op ping --listen
python -m agent.loop --goal "查看当前场景"
python -m agent.loop --goal "创建一个名为 Cup 的球体"
python -m agent.loop --goal "预览当前场景"
```

`--listen` 会在指令返回后保持连接。此时在视口里选中或移动物体，终端会打印 `user_focus` 与 `object_transformed`。`agent.loop` 会自己走「读场景 → 调用指令 → 反思」循环。创建成功后，或目标里写了预览，它会先要一张 Workbench 黏土预览。图小于 64 像素时不算通过，循环会改用 Eevee 再渲染一次，通过后才写入 `previews/`。模拟端和真实插件不要同时占用 `blender` 这个角色，后连上的一方会替换先连上的一方。

若插件面板提示找不到 `protocol`，确认链接的是仓库里的 `blenderai_os` 目录（其向上两级应是仓库根）。也可以在启动 Blender 前设置 `BLENDERAI_ROOT` 指向仓库根目录。

## 这一步已经接通的能力

- 统一场景上下文的第一刀：`get_scene` 序列化物体树、灯光、相机和材质节点摘要。
- 双向事件：Agent 发原子指令，Blender 用 `bpy.app.handlers.depsgraph_update_post` 感知选择与位移并回推事件。
- 两端握手：`hello` / `hello_ack` / `peer_status`。
- Agent 循环：`python -m agent.loop` 读取场景树文本，调用白名单指令，再根据结果决定是否复核或停止。
- 预览验收：`render_preview` 先用 Workbench 渲染。Agent 检查 PNG 尺寸和体积，不合格就改用 Eevee 再渲染一次，通过后才保存到 `previews/`。

不要把中枢暴露到局域网之外。
