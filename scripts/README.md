# Scripts

按**能力域**和**SoC 板型**两级组织。`device/` 和 `mcp/` 是板型无关的通用工具；`soc/` 下的每个子目录绑定一个 SoC 家族，其中沉淀的知识（VPU 接口、构建参数、厂商库行为）只适用于该芯片。

## 目录结构

```
scripts/
├── device/                  # 设备交互（板型无关）
│   ├── cmd.py              # SSH 执行命令
│   └── get.py              # SFTP 下载文件
├── mcp/                     # MCP 测试（板型无关）
│   └── smoke.py            # 持久会话冒烟
└── soc/
    └── allwinner-a733/      # Allwinner AW2511 / A733 家族专属
        ├── vpu-decode      # GStreamer OMX 解码 CLI
        └── ffmpeg/          # ffmpeg-vpu 源码与构建
            ├── cedar_dec.c
            └── build.sh
```

## 新增 SoC 的规则

不同 SoC 的 VPU 接口完全不同（CedarC vs Rockchip MPP vs Amlogic V4L2）。
新板子的 SoC 专属工具放到 `soc/<soc-name>/` 下，不要混入 `device/`。
这样 agent 接手时看目录名就知道适用范围，不会拿 CedarC 的思路去套 RK3588。

| SoC 目录 | 芯片家族 | VPU 接口 | 已验证板 |
|---------|---------|---------|---------|
| `soc/allwinner-a733/` | AW2511 / A733 | CedarC (`libvdecoder`) | Radxa Cubie A7A |

## 设备工具

```bash
python scripts/device/cmd.py --device 192.168.0.142 "uname -a"
python scripts/device/get.py --device 192.168.0.142 /tmp/dmesg.log .
```

## MCP 测试

```bash
python scripts/mcp/smoke.py --host 192.168.0.142 --username radxa
```
