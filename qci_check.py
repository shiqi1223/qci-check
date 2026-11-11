#!/usr/bin/env python3
"""一键查电话卡的 QCI（网络优先级）。

适用：高通芯片、已 root 的安卓手机，USB 调试连电脑。在一加 Ace 3V（SM7675，ColorOS 16）上验证过。
1. 先试不用 root 的办法：系统 dumpsys 里记录的默认 QoS。
2. 读不到就用 root：用手机自带的高通日志工具 diag_mdlog 加厂商预置的日志配置抓基带日志。
   期间先临时切到只用 4G：手机在 5G 上时，会把 5G 会话按网络给的映射参数转成 4G 承载，
   基带打印 "QCI of EPS LTE bearer X is Y"，这个值和 5G 下的 5QI 按标准一一对应。
   再开关一次飞行模式，在 4G 下重新注册，取底层配置里的 QCI 作对照。
3. 结束后恢复网络制式和飞行模式，删除手机上的临时文件。
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE_DIR = '/data/local/tmp/qci'
DIAG_MDLOG = '/vendor/bin/diag_mdlog'
CFG_CANDIDATES = ['/odm/etc/modem_data.cfg', '/odm/etc/Diag.cfg']

# TelephonyManager 的网络类型名 -> 类型编号（对应的位 = 1 << (编号 - 1)）
NETWORK_TYPES = {
    'GPRS': 1, 'EDGE': 2, 'UMTS': 3, 'CDMA': 4, 'CDMA - EvDo rev. 0': 5,
    'CDMA - EvDo rev. A': 6, 'CDMA - 1xRTT': 7, 'HSDPA': 8, 'HSUPA': 9, 'HSPA': 10,
    'iDEN': 11, 'CDMA - EvDo rev. B': 12, 'LTE': 13, 'CDMA - eHRPD': 14, 'HSPA+': 15,
    'GSM': 16, 'TD_SCDMA': 17, 'IWLAN': 18, 'LTE_CA': 19, 'NR': 20,
}

# 推到手机上、用 root 在后台运行的脚本。用 setsid 脱离 adb，数据线中途断开也会自己恢复设置。
DEVICE_SCRIPT = r'''#!/system/bin/sh
SLOT=$1
ORIG=$2
CFG=$3
D=/data/local/tmp/qci
OUT=$D/logs
LTE_ONLY=1000001000000000000

restore() {
  /vendor/bin/diag_mdlog -k > /dev/null 2>&1
  cmd phone set-allowed-network-types-for-users -s "$SLOT" "$ORIG" > /dev/null 2>&1
  cmd connectivity airplane-mode disable > /dev/null 2>&1
}
trap restore EXIT
trap 'exit 1' HUP INT TERM

rm -rf "$OUT" "$D/done"
mkdir -p "$OUT"
chmod 777 "$D" "$OUT"   # diag_mdlog 会降成 shell 用户运行，目录要可写
/vendor/bin/diag_mdlog -f "$CFG" -o "$OUT" -c > "$D/mdlog.out" 2>&1 &
sleep 4
echo "4G only"
cmd phone set-allowed-network-types-for-users -s "$SLOT" "$LTE_ONLY"
sleep 6
echo "airplane on"
cmd connectivity airplane-mode enable
sleep 4
echo "airplane off"
cmd connectivity airplane-mode disable
i=0
while [ $i -lt 30 ]; do
  case "$(getprop gsm.network.type)" in *LTE*) break ;; esac
  sleep 1
  i=$((i + 1))
done
sleep 10
echo "net=$(getprop gsm.network.type)" > "$D/net.txt"
restore
trap - EXIT
sleep 2
chmod -R 777 "$OUT"
echo ok > "$D/done"
'''


# ---------------- adb ----------------

def squote(s):
    return "'" + s.replace("'", "'\\''") + "'"


class Adb:
    def __init__(self, path, serial=None):
        self.base = [path] + (['-s', serial] if serial else [])

    def run(self, *args, timeout=60):
        r = subprocess.run(self.base + list(args), capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode('utf-8', 'replace').replace('\r', '')

    def sh(self, command, timeout=60):
        return self.run('shell', command, timeout=timeout)[1]

    def su(self, command, timeout=60):
        return self.sh('su -c ' + squote(command), timeout=timeout)


def find_adb(user_path=None):
    """依次找：--adb 参数、PATH、ANDROID_HOME / ANDROID_SDK_ROOT、Android Studio 默认位置、各盘根目录的 Android\\Sdk。"""
    cands = [user_path, shutil.which('adb')]
    for env in ('ANDROID_HOME', 'ANDROID_SDK_ROOT'):
        if os.environ.get(env):
            cands.append(os.path.join(os.environ[env], 'platform-tools', 'adb.exe'))
    cands.append(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk', 'platform-tools', 'adb.exe'))
    cands += [rf'{d}:\Android\Sdk\platform-tools\adb.exe' for d in 'CDEFGH']
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def pick_device(adb_path, serial):
    out = subprocess.run([adb_path, 'devices'], capture_output=True, timeout=30).stdout.decode('utf-8', 'replace')
    rows = [line.split('\t') for line in out.splitlines()[1:] if '\t' in line]
    ready = [s for s, state in rows if state.strip() == 'device']
    if serial:
        return serial if serial in ready else None
    if not ready:
        if any(state.strip() == 'unauthorized' for _, state in rows):
            print('手机上弹出的「允许 USB 调试」还没点允许。')
        else:
            print('没检测到手机：请打开 USB 调试并用数据线连上电脑。')
        return None
    if len(ready) > 1:
        print('连了多台手机，请用 --serial 指定其中一台：', ', '.join(ready))
        return None
    return ready[0]


def detect_slot(adb):
    """取上网卡所在的卡槽，取不到就用 0。"""
    txt = adb.sh('dumpsys isub')
    m = re.search(r'defaultDataSubId=(\d+)', txt)
    if m:
        m2 = re.search(r'SubscriptionInfoInternal: id=%s\b[^\]]*?simSlotIndex=(-?\d+)' % m.group(1), txt)
        if m2 and int(m2.group(1)) >= 0:
            return int(m2.group(1))
    return 0


def no_root_qos(adb):
    """系统记录的各数据连接的默认 QoS：[(APN, QCI, 5QI)]，基带不上报时都是 None。"""
    txt = adb.sh('dumpsys telephony.registry')
    res = {}
    for seg in txt.split('APN Setting: ')[1:]:
        m = re.match(r'\[ApnSetting\] [^,]*, [^,]*, [^,]*, ([^,]*)', seg)
        q = re.search(r'default QoS: (null|EpsQos \{[^}]*|NrQos \{[^}]*)', seg)
        if not m or not q:
            continue
        qci = re.search(r'(?:qosClassId|qci)=(\d+)', q.group(1))
        fqi = re.search(r'fiveQi=(\d+)', q.group(1))
        res[m.group(1).strip()] = (int(qci.group(1)) if qci else None, int(fqi.group(1)) if fqi else None)
    return [(apn, qci, fqi) for apn, (qci, fqi) in res.items()]


def get_allowed(adb, slot):
    out = adb.sh(f'cmd phone get-allowed-network-types-for-users -s {slot}').strip()
    names = [x.strip() for x in out.split('|') if x.strip()]
    if not names:
        raise RuntimeError(f'读不到当前的网络制式设置：{out!r}')
    mask = 0
    for n in names:
        if n not in NETWORK_TYPES:
            raise RuntimeError(f'不认识的网络类型 {n!r}，为安全起见不改设置')
        mask |= 1 << (NETWORK_TYPES[n] - 1)
    return names, mask


def ensure_restored(adb, slot, orig_names, orig_mask):
    """确认网络制式和飞行模式都已恢复，没恢复就再设一次。返回是否正常。"""
    for _ in range(3):
        try:
            names, _ = get_allowed(adb, slot)
        except RuntimeError:
            names = None
        airplane_on = 'enabled' in adb.sh('cmd connectivity airplane-mode')
        if names == orig_names and not airplane_on:
            return True
        adb.su(f'cmd phone set-allowed-network-types-for-users -s {slot} {orig_mask:b}')
        adb.su('cmd connectivity airplane-mode disable')
        time.sleep(2)
    return False


def capture(adb, slot, cfg, orig_mask, workdir):
    """在手机上后台跑抓取脚本，等它结束。返回是否正常结束。"""
    adb.sh(f'rm -rf {DEVICE_DIR}; mkdir -p {DEVICE_DIR}')
    local = os.path.join(workdir, 'capture.sh')
    with open(local, 'w', encoding='utf-8', newline='\n') as f:
        f.write(DEVICE_SCRIPT)
    adb.run('push', local, f'{DEVICE_DIR}/capture.sh')
    launch = (f'setsid sh {DEVICE_DIR}/capture.sh {slot} {orig_mask:b} {cfg} '
              f'> {DEVICE_DIR}/capture.log 2>&1 < /dev/null &')
    try:
        adb.su(launch, timeout=30)
    except subprocess.TimeoutExpired:
        pass  # 脚本已在手机上跑起来，继续等结果即可
    start = time.time()
    while time.time() - start < 150:
        time.sleep(3)
        if adb.sh(f'cat {DEVICE_DIR}/done 2>/dev/null').strip() == 'ok':
            print(f'\r  抓取完成，用时 {int(time.time() - start)} 秒      ')
            return True
        print(f'\r  抓取中… {int(time.time() - start)} 秒', end='', flush=True)
    print()
    return False


def pull_logs(adb, workdir):
    names = adb.sh(f'ls {DEVICE_DIR}/logs').split()
    qmdl, qdb = [], []
    for n in names:
        if n.endswith(('.qmdl', '.qmdl2', '.qdb')):
            local = os.path.join(workdir, n)
            adb.run('pull', f'{DEVICE_DIR}/logs/{n}', local, timeout=300)
            (qdb if n.endswith('.qdb') else qmdl).append(local)
    return qmdl, qdb


# ---------------- 日志解析 ----------------

# 要找的打印：带 QCI / 5QI 的，加上飞行模式切换（用来区分「5G 映射」和「4G 重新注册」两个阶段）
WANTED = re.compile(r'qci|5qi|oprt_chgd: oprt_mode', re.I)


def load_templates(qdb_paths):
    """从 QShrink4 字典（.qdb：64 字节头 + zlib 文本）里取出要找的打印模板。"""
    templates = {}
    for path in qdb_paths:
        raw = open(path, 'rb').read()
        if raw[:4] != b'\x7fQDB':
            continue
        for line in zlib.decompress(raw[64:]).split(b'\n'):
            head, sep, rest = line.partition(b':')
            if not sep or not head.isdigit() or int(head) <= 0xFFFF:  # 太小的编号容易误命中
                continue
            text = rest.decode('latin1').rstrip('\r ')
            if WANTED.search(text):
                parts = text.split(':', 4)
                templates[int(head)] = parts[4] if len(parts) == 5 else text
    return templates


def find_prints(qmdl_paths, templates):
    """按时间顺序返回 [(模板, 参数)]。只看 0x9D（Mtrace）包，
    记录结构：[0x13+参数个数][3 字节][时间戳 4][模板编号 4][参数 4×n]。"""
    if not templates:
        return []
    pats = {struct.pack('<I', h): h for h in templates}
    rx = re.compile(b'|'.join(re.escape(p) for p in pats))
    hits = []
    for path in qmdl_paths:
        data = open(path, 'rb').read()
        if path.endswith('.qmdl2') and len(data) > 8:
            data = data[struct.unpack_from('<I', data, 0)[0]:]
        for frame in data.split(b'\x7e'):
            if len(frame) < 16:
                continue
            p = frame.replace(b'\x7d\x5e', b'\x7e').replace(b'\x7d\x5d', b'\x7d')
            if p[0] != 0x9D:
                continue
            for m in rx.finditer(p):
                fmt = templates[pats[m.group()]]
                n = fmt.count('%')
                pos = m.start()
                if pos < 8 or p[pos - 8] - 0x13 != n or m.end() + 4 * n > len(p):
                    continue
                hits.append((fmt, struct.unpack_from(f'<{n}i', p, m.end())))
    return hits


def fill(fmt, args):
    it = iter(args)
    return re.sub(r'%[-+ #0-9.]*[a-zA-Z]', lambda _: str(next(it, '?')), fmt)


def describe(q):
    if q == 5:
        return 'IMS 信令（VoLTE 通话用，所有人都是 5）'
    if q in (1, 2):
        return 'VoLTE 语音' if q == 1 else '视频通话'
    if 6 <= q <= 9:
        return '上网数据'
    return '运营商自定义' if q >= 128 else '其他'


def valid(q):
    return 0 < q < 255  # 255 是承载还没建好时的占位值


def report(hits, start_net=''):
    """mapped：飞行模式前，5G 会话映射成 4G 承载时的 QCI；reattach：关飞行模式、4G 重新注册后的 QCI。"""
    phase, saw_airplane = 'before', False
    mapped, reattach, five_qi = {}, set(), []
    for fmt, args in hits:
        if 'oprt_chgd: oprt_mode' in fmt and len(args) >= 2:
            if (args[0], args[1]) == (5, 6):
                phase, saw_airplane = 'airplane', True
            elif (args[0], args[1]) == (6, 5):
                phase = 'after'
            continue
        if 'QCI of EPS LTE bearer' in fmt or 'QOS_INFO_IND' in fmt:
            if not valid(args[1]):
                continue
            if phase == 'before':
                if 'QOS_INFO_IND' in fmt:
                    mapped.setdefault(args[0], args[1])  # 以 DS 模块那条为准
                else:
                    mapped[args[0]] = args[1]
            elif phase == 'after':
                reattach.add(args[1])
        elif 'Send LC Qci Info Ind' in fmt and phase == 'after' and valid(args[1]):
            reattach.add(args[1])
        elif re.search('5qi', fmt, re.I) and not re.search('qci', fmt, re.I):
            five_qi.append(fill(fmt, args))

    if not mapped and not reattach:
        print('没找到 QCI 打印：可能手机没在 4G 下重新注册成功，可以再跑一次。')
        return False
    from_5g = 'NR' in start_net or not start_net
    print('结果：')
    if mapped:
        title = '5G 会话映射成 4G 承载时的 QCI（与 5QI 一一对应）' if from_5g and saw_airplane else '4G 承载的 QCI'
        print(f'  {title}：')
        for b in sorted(mapped):
            print(f'    承载 {b}：QCI {mapped[b]:<3d} {describe(mapped[b])}')
    elif saw_airplane and not from_5g:
        print('  开始时手机不在 5G 上，所以没有 5G 映射值。')
    if reattach:
        print(f'  在 4G 重新注册后的 QCI：{" / ".join(map(str, sorted(reattach)))}')
    data = sorted({q for q in list(mapped.values()) + list(reattach) if q >= 6})
    if data:
        print(f'→ 上网 QCI = {" / ".join(map(str, data))}')
        if mapped and from_5g and saw_airplane:
            print('  5G 下的 5QI 读不到原值（基带把相关日志加密了），但按标准和上面的映射值一一对应。')
    print('说明：普通上网的 QCI 在 6–9 之间，数字越小越优先；9 是普通用户的默认档。')
    for line in dict.fromkeys(five_qi):
        print('  5QI 相关打印：', line)
    return True


def parse_logs(qmdl_paths, qdb_paths):
    return find_prints(qmdl_paths, load_templates(qdb_paths))


# ---------------- 主流程 ----------------

def keep(paths, tag):
    dest = os.path.join(HERE, 'logs', time.strftime('%Y%m%d_%H%M%S') + tag)
    os.makedirs(dest, exist_ok=True)
    for p in paths:
        shutil.copy2(p, dest)
    return dest


def main():
    for stream in (sys.stdout, sys.stderr):  # 输出被重定向到文件时也能打中文
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description='一键查电话卡 QCI（高通 + root 安卓手机）')
    ap.add_argument('--adb', help='adb.exe 路径（默认自动找）')
    ap.add_argument('--serial', help='连了多台手机时指定序列号')
    ap.add_argument('--slot', type=int, help='卡槽 0 或 1（默认取上网卡所在卡槽）')
    ap.add_argument('-y', '--yes', action='store_true', help='不再询问，直接开始')
    ap.add_argument('--keep-logs', action='store_true', help='把抓到的日志存到 logs 文件夹')
    ap.add_argument('--root-only', action='store_true', help='跳过不用 root 的检查')
    ap.add_argument('--parse', nargs=2, metavar=('QMDL', 'QDB'), help='只解析已有的日志，不连手机')
    a = ap.parse_args()

    if a.parse:
        return 0 if report(parse_logs([a.parse[0]], [a.parse[1]])) else 1

    adb_path = find_adb(a.adb)
    if not adb_path:
        print('找不到 adb，请装 Google 的 platform-tools，或用 --adb 指定路径。')
        return 1
    serial = pick_device(adb_path, a.serial)
    if not serial:
        return 1
    adb = Adb(adb_path, serial)
    model = adb.sh('getprop ro.product.model').strip()
    slot = a.slot if a.slot is not None else detect_slot(adb)
    ops = adb.sh('getprop gsm.sim.operator.alpha').strip().split(',')
    nets = adb.sh('getprop gsm.network.type').strip().split(',')
    start_net = nets[slot] if slot < len(nets) else ''
    print(f'手机：{model}（{serial}）  卡槽：{slot}')
    print(f'运营商：{ops[slot] if slot < len(ops) else "?"}  当前网络：{start_net or "?"}')

    if not a.root_only:
        print('\n[1/3] 先试不用 root 的办法…')
        found = [x for x in no_root_qos(adb) if x[1] or x[2]]
        if found:
            for apn, qci, fqi in found:
                print(f'  {apn}：' + ('QCI %d  ' % qci if qci else '') + ('5QI %d' % fqi if fqi else ''))
            print('  （这台手机的基带会上报 QoS，不需要 root。）')
            return 0
        print('  系统里没有记录（基带不上报），改用 root 方式。')

    print('\n[2/3] 用 root 抓基带日志…')
    if 'uid=0' not in adb.su('id', timeout=40):
        print('  拿不到 root：请在手机的 Magisk 里给「Shell」授权超级用户。')
        return 1
    if DIAG_MDLOG not in adb.su(f'ls {DIAG_MDLOG}'):
        print('  手机上没有高通日志工具 diag_mdlog，这个方法用不了。')
        return 1
    cfg = next((c for c in CFG_CANDIDATES if c in adb.su(f'ls {c}')), None)
    if not cfg:
        print('  手机上没找到厂商的基带日志配置（/odm/etc/modem_data.cfg），这个方法用不了。')
        return 1
    try:
        orig_names, orig_mask = get_allowed(adb, slot)
    except RuntimeError as e:
        print('  ' + str(e))
        return 1

    print('  接下来会临时只用 4G，并开关一次飞行模式，手机断网约 40 秒，结束后自动恢复。')
    if not a.yes and input('  继续吗？(Y/n) ').strip().lower() not in ('', 'y', 'yes'):
        print('  已取消。')
        return 0

    workdir = tempfile.mkdtemp(prefix='qci_')
    ok = False
    try:
        ok = capture(adb, slot, cfg, orig_mask, workdir)
    except KeyboardInterrupt:
        print('\n  已中断，正在恢复设置…')
    finally:
        restored = ensure_restored(adb, slot, orig_names, orig_mask)
        print('  已恢复：网络制式和之前一致，飞行模式已关。' if restored
              else '  ⚠ 没能自动恢复，请在手机设置里关掉飞行模式，并把网络制式改回 5G。')
    if not ok:
        print('  抓取没有正常结束，手机上的临时文件留在', DEVICE_DIR)
        return 1

    net = adb.sh(f'cat {DEVICE_DIR}/net.txt').strip().replace('net=', '')
    print(f'  测试时网络：{net}')
    qmdl, qdb = pull_logs(adb, workdir)
    adb.sh(f'rm -rf {DEVICE_DIR}')

    print('\n[3/3] 解析日志…')
    found = report(parse_logs(qmdl, qdb), start_net)
    if a.keep_logs or not found:
        print('  日志已保存到', keep(qmdl + qdb, '' if found else '_未找到'))
    shutil.rmtree(workdir, ignore_errors=True)
    return 0 if found else 1


if __name__ == '__main__':
    sys.exit(main())
