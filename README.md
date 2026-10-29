# fnos-intel-powerctl

飞牛 fnOS（Intel 平台）CPU 功耗控制 fpk 应用。参考 [RyzenAdj-for-fnOS](https://github.com/LANMIN-X/RyzenAdj-for-fnOS) 的思路，为 Intel CPU 实现等价能力。

> 适配机型示例：HP EliteDesk 800 G5 SFF（i5-9500），同样适用于其他支持 RAPL MSR 的 Intel 处理器（约 Haswell 及以后）。

## 功能

- **功耗墙调节**：PL1（持续功耗）/ PL2（短时睿频功耗）/ 时间窗 tau
- **能效偏好（HWP EPP）**：0（最高性能）~ 255（最省电），写 `IA32_HWP_REQUEST` bit31:24
- **HWP 寄存器诊断**：Min/Max/Desired/EPP 四字段可视化，可一键清除旧版残留的 Minimum_Performance
- **预设方案**：省电静音 / 均衡推荐 / 性能优先 / 满血释放
- **实时监控**：封装功耗（RAPL 能耗计数器）、CPU 温度、全核平均频率
- **开机恢复**：设置持久化到 `/var/lib/intel-powerctl/config.json`，重启后自动应用
- **一键还原**：恢复到应用首次启动时记录的原始值

## 原理

RyzenAdj 走 AMD SMU，Intel 平台的等价机制是 RAPL MSR 寄存器：

| MSR | 作用 |
|-----|------|
| `0x606` | RAPL_POWER_UNIT 功耗/时间最小单位 |
| `0x610` | PKG_POWER_LIMIT PL1/PL2/tau |
| `0x611` | PKG_ENERGY_STATUS 能耗计数器（算实时功耗） |
| `0x1FC` | ENERGY_PERF_BIAS **仅读取展示，不写**（HWP 开启后硬件忽略） |
| `0x770` | PM_ENABLE HWP 总开关（判断 EPP 是否生效） |
| `0x774` | HWP_REQUEST bit31:24 = EPP（真正生效的能效旋钮） |

写 MSR 需要 root（fpk 已声明 `run-as=root`）。

## 安装

1. 飞牛应用中心 → 手动安装 → 上传 `dist/intel-powerctl-1.0.0.fpk`
2. 若服务显示 MSR 不可用，SSH 执行：

```bash
sudo modprobe msr
```

（如需开机自动加载：`echo msr | sudo tee /etc/modules-load.d/msr.conf`）

3. 打开应用 → Web 界面默认端口 **59500**

## API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 当前功耗墙/实时功耗/温度/频率 |
| `/api/apply` | POST | `{pl1, pl2, tau, epp}` 应用并持久化（pl1<25W 会返回 warn） |
| `/api/preset` | POST | `{name: "均衡推荐"}` 等预设 |
| `/api/reset` | POST | 还原到启动时原始值 |
| `/api/original` | GET | 查看记录的原始值 |

## 注意

- 部分 OEM 机器（如 HP 商用机）BIOS 可能锁定功耗墙或限制 MSR 写入（`MSRLock`），写入失败会返回明确错误
- 满血释放前请确认散热条件；长期高功耗墙影响 7×24 噪音与寿命，NAS 建议用「均衡推荐 65W」

## NAS 调参建议（i5-9500 等 6 核平台）

| 项目 | 建议 | 原因 |
|------|------|------|
| PL1 下限 | **≥ 25W**（本应用默认 35W 省电档 / 45W 均衡档） | 压到 25W 以下六核满载会掉到 2.2GHz 以下，Jellyfin 转码（解码/滤镜/音频仍走 CPU）会卡，而省电只有 3~4W |
| 能效旋钮 | 用 **EPP**，不要用 EPB | HWP 模式下硬件忽略 `0x1FC`，内核 `intel_pstate` 也只读 EPP；写 EPB 只是心理安慰 |
| PCIe ASPM | 保持 **default**，不要改 powersupersave | default 下 L1 已按设备能力启用；powersupersave 收益 <1W，但会让 SATA/NVMe/网卡进 L1.2，NAS 上掉盘/掉网卡风险远大于收益 |
| 睿频 | 一般不用关 | 关掉后单核锁 3.0GHz，交互响应明显变慢；想省电优先调 PL1 与 EPP |

> 本应用自 v1.0.12 起：PL1 低于 25W 会在界面提示（不硬拦）；不再写 EPB；ASPM 改为只读展示并从接口层屏蔽 `powersupersave`。

## License

MIT
