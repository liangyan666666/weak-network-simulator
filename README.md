# 弱网环境模拟器 (Weak Network Simulator)

基于 **WinDivert** 的 Windows 弱网测试工具。可对全局或指定进程的 TCP/UDP 流量施加**上行/下行**的网络损伤,用于客户端弱网表现测试、协议健壮性验证等场景。

![Language](https://img.shields.io/badge/language-Python%203.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## 功能特性

- **按进程 / 全局模式**:默认全局生效;也可从进程列表选择或点击 🎯 拾取窗口来锁定目标进程
- **上行 / 下行独立配置**,可分别设置:
  - 延时 (ms)
  - 延时抖动 (ms)
  - 带宽限速 (B/s、KB/s、MB/s)
  - 随机丢包 (%)
  - 周期连丢(放行 N 个 → 丢弃 M 个)
- **协议过滤**:UDP / TCP / TCP+UDP
- **自动恢复**:设定时长(秒)后自动恢复网络,或勾选"不自动恢复网络"
- **快捷键**:`HOME` 隐藏/显示窗口;自定义键盘或鼠标键作为启停热键(支持鼠标侧键)
- **配置持久化**:参数与热键可保存到 `config.json`(运行目录下自动生成)
- **打包发布**:支持 PyInstaller 打包为单 exe(自动请求管理员权限)

## 环境要求

- Windows 10 / 11
- Python 3.9+
- **必须以管理员身份运行**(需要加载 WinDivert 过滤驱动)
- 若安全软件拦截 WinDivert 驱动,请在防火墙/杀软中放行

## 安装与运行

```bash
git clone https://github.com/liangyan666666/weak-network-simulator.git
cd weak-network-simulator

# 创建虚拟环境并安装依赖(Windows)
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 方式一:双击「启动弱网模拟器.bat」(自动请求管理员权限)
# 方式二:命令行提权后运行
.venv\Scripts\python weak_net_simulator.py
```

## 使用说明

1. 启动后默认勾选**全局模式**,对所有进程流量生效;如需指定进程,从下拉框选择或点击 🎯 用鼠标拾取目标窗口
2. 按需填写上行 / 下行参数(默认全为 0,即不施加弱网效果)
3. 点击「启 动」开始模拟,再次点击「停 止」恢复网络
4. 设置好参数后可点「保存配置」,下次启动自动加载

### 快捷键

| 按键 | 功能 |
|---|---|
| `HOME` | 隐藏 / 显示窗口 |
| 自定义热键 | 启动 / 停止弱网(界面「修改绑定」,支持键盘键或鼠标键) |

## 打包为单 exe

```bash
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller --onefile --windowed --uac-admin --name "弱网模拟器" --collect-all pydivert --noconfirm weak_net_simulator.py
```

产物位于 `dist\弱网模拟器.exe`,单文件、无控制台、双击自动弹 UAC 提权,无需安装 Python。

> 项目内附 `弱网模拟器.spec`,也可直接 `.venv\Scripts\pyinstaller 弱网模拟器.spec` 复现打包。

## 技术原理

- 使用 **pydivert (WinDivert)** 在 NETWORK 层挂钩网络流量,对经过的 TCP/UDP 包执行延时、抖动、令牌桶限速、随机丢包与周期连丢整形,停止时自动排空缓冲,避免网络卡死
- NETWORK 层不提供进程信息,故另开 **FLOW 层**句柄维护五元组 → PID 映射,实现按进程过滤;未匹配到进程的包一律放行,不误伤其他流量
- 回环(Loopback)包直接放行,避免影响本机代理与回环服务

## 免责声明

本项目仅用于**合法的网络测试、开发调试与学习研究**。请遵守所在地区法律法规,勿将其用于干扰他人网络、绕过服务限制等非法用途。使用本工具造成的一切后果由使用者自行承担。
