# qci-check：一键查电话卡 QCI

Read your SIM's QCI (LTE QoS class) on a rooted Qualcomm Android phone, using the modem's own diag logs. Windows + adb.

QCI 是运营商给每张卡分配的网络优先级，基站拥堵时（演唱会、车站等）按它决定先服务谁。普通上网的 QCI 在 6–9 之间，数字越小越优先，9 是普通用户的默认档。

## 用法

1. 手机打开 USB 调试，用数据线连电脑。电脑上要有 adb（Google 的 platform-tools），并装好 Python 3。
2. 双击 `查QCI.bat`。第一次用 root 时，手机上的 Magisk 会弹窗问是否给「Shell」授权，点允许。
3. 大约 1 分钟后，窗口里会显示结果，例如：

```
结果：
  5G 会话映射成 4G 承载时的 QCI（与 5QI 一一对应）：
    承载 5：QCI 5   IMS 信令（VoLTE 通话用，所有人都是 5）
    承载 6：QCI 9   上网数据
  在 4G 重新注册后的 QCI：9
→ 上网 QCI = 9
```

## 原理

1. 先试不用 root 的办法：读 `dumpsys telephony.registry` 里的默认 QoS。很多高通手机的基带不上报这个值，读出来是空的。
2. 读不到就用 root：调用手机自带的高通日志工具 `/vendor/bin/diag_mdlog`，配合厂商预置的日志配置 `/odm/etc/modem_data.cfg` 抓基带日志，期间：
   - **临时只用 4G**。手机原本在 5G 时，不会重新注册，而是把 5G 会话按网络下发的映射参数直接转成 4G 承载。这时基带打印 `QCI of EPS LTE bearer X is Y`，这个值和 5G 下的 5QI 按标准一一对应。
   - **开关一次飞行模式**，在 4G 下重新注册，取底层配置里的 QCI 作对照。
3. 基带的调试打印是压缩过的，只存模板编号。脚本用日志里附带的 QShrink 字典（`.qdb`）把它们还原成文字。
4. 结束后自动恢复网络制式、关掉飞行模式，并删除手机上的临时文件。手机端脚本独立运行，就算数据线中途断开，也会自己恢复。

## 已知限制

- 只在一加 Ace 3V（高通 SM7675，ColorOS 16，Magisk root）上验证过。其他 OPPO / 一加 / 真我机型，只要有同样的配置文件，大概率也能用。
- 读不到 5G 下 5QI 的原值：新款高通基带会把信令日志用 RSA-2048 公钥加密，私钥只在高通 / 厂商手里。脚本给出的是网络下发的映射值，按标准与 5QI 一一对应。
- 同理，NSG、QCSuper、SCAT 这类靠解析信令的工具，在这类手机上大概率也读不到 QCI。

## 注意

- 运行时手机会**断网约 40 秒**，期间接不到电话。
- 加 `--keep-logs` 会把抓到的基带日志存到 `logs` 文件夹。日志里有基站、位置等信息，不要公开分享。
- 需要 root，会临时修改网络设置，请自行承担使用风险。

## 其他参数

```bash
python qci_check.py --yes          # 不询问直接开始
python qci_check.py --keep-logs    # 把抓到的日志存到 logs 文件夹
python qci_check.py --slot 1       # 指定卡槽（默认取上网卡所在卡槽）
python qci_check.py --adb 路径      # 指定 adb.exe 位置（默认自动找）
python qci_check.py --parse 日志.qmdl 字典.qdb   # 只解析已有日志
```

## 许可证

MIT
