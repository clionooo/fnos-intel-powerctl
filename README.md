# fnos-intel-powerctl

飞牛 fnOS（Intel 平台）CPU 功耗控制 fpk 应用。参考 [RyzenAdj-for-fnOS](https://github.com/LANMIN-X/RyzenAdj-for-fnOS) 的思路，为 Intel CPU 实现等价能力。

> 适配机型示例：HP EliteDesk 800 G5 SFF（i5-9500），同样适用于其他支持 RAPL MSR 的 Intel 处理器（约 Haswell 及以后）。

## 功能

- **功耗墙调节**：PL1（持续功耗）/ PL2（短时睿频功耗）/ 时间窗 tau
- **能效模式（EPB）**：0（最高性能）~ 15（最省电）
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
| `0x1FC` | ENERGY_PERF_BIAS 能效偏好 |

写 MSR 需要 root（fpk 已声明 `run-as=root`）。

## 安装

1. 飞牛应用中心 → 手动安装 → 上传 `dist/intel-powerctl-1.0.0.fpk`
2. 若服务显示 MSR 不可用，SSH 执行：

```bash
sudo modprobe msr
```

（如需开机自动加载：`echo msr | sudo tee /etc/modules-load.d/msr.conf`）

3. 打开应用 → Web 界面默认端口 **9527**

## API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 当前功耗墙/实时功耗/温度/频率 |
| `/api/apply` | POST | `{pl1, pl2, tau, epb}` 应用并持久化 |
| `/api/preset` | POST | `{name: "均衡推荐"}` 等预设 |
| `/api/reset` | POST | 还原到启动时原始值 |
| `/api/original` | GET | 查看记录的原始值 |

## 注意

- 部分 OEM 机器（如 HP 商用机）BIOS 可能锁定功耗墙或限制 MSR 写入（`MSRLock`），写入失败会返回明确错误
- 满血释放前请确认散热条件；长期高功耗墙影响 7×24 噪音与寿命，NAS 建议用「均衡推荐 65W」

## License

MIT
