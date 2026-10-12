from threading import Thread, Event
import os
import time
import glob
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

def parse_range(s):
    s = s.strip()
    if '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end) + 1))
    else:
        return [int(s)]

def read_config(config_file):
    print(f"读取设置文件：{config_file}")
    ip_configs = []
    try:
        with open(config_file, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(',')
                ip_port_part = parts[0].strip()
                url_flag = int(parts[1])
                url_end = "/stat" if url_flag == 1 else "/status"
                max_find = int(parts[2]) if len(parts) >= 3 else None
                if ':' not in ip_port_part:
                    print(f"第{line_num}行格式错误，缺少端口分隔符: {line}")
                    continue
                ip_part, port_str = ip_port_part.split(':', 1)
                ports = [p.strip() for p in port_str.split('|') if p.strip()]
                if not ports:
                    print(f"第{line_num}行未识别到有效端口: {line}")
                    continue
                dot_count = ip_part.count('.')
                for port in ports:
                    if dot_count == 3:
                        segs = ip_part.split('.')
                        a_seg, b_seg, c_seg, d_seg = segs
                        ip_configs.append(
                            ((a_seg, b_seg, c_seg, d_seg), port, url_end, "range4", max_find)
                        )
                    elif dot_count == 0:
                        first_seg = ip_part
                        ip_configs.append(
                            (first_seg, port, url_end, "full", max_find)
                        )
                    else:
                        a, b, c, d = ip_part.split('.')
                        ip = f"{a}.{b}.1.1"
                        ip_configs.append(
                            (ip, port, url_end, "old", max_find)
                        )
                print(
                    f"第{line_num}行：{ip_part} 端口 {port_str}，"
                    f"模式={url_flag}({url_end})，最多获取 {max_find} 个"
                )
        return ip_configs
    except Exception as e:
        print(f"读取文件错误: {e}")
        return None

def check_ip_port(ip_port, url_end, stop_event):
    if stop_event.is_set():
        return None
    try:
        url = f"http://{ip_port}{url_end}"
        resp = requests.get(url, timeout=3)
        resp.raise_for_status()
        if "Multi stream daemon" in resp.text or "udpxy status" in resp.text:
            print(f"✅ {url} 访问成功")
            return ip_port
    except Exception as e:
        # 调试打印，大范围扫描注释掉，防止日志刷屏
        # print(f"❌ {url} 失败: {str(e)[:70]}")
        return None

def scan_ip_port(ip_info, port, url_end, mode, max_find):
    stop_event = Event()
    def show_progress():
        while not stop_event.is_set() and checked[0] < len(ip_ports):
            print(f"已扫描：{checked[0]}/{len(ip_ports)}, 有效ip_port：{len(valid_ip_ports)}个")
            time.sleep(20)
    valid_ip_ports = []
    ip_ports = []
    if mode == "full":
        first = ip_info
        for x in range(1, 101):
            for y in range(1, 101):
                for z in range(1, 101):
                    ip_ports.append(f"{first}.{x}.{y}.{z}:{port}")
    elif mode == "range4":
        a_seg, b_seg, c_seg, d_seg = ip_info
        a_list = parse_range(a_seg)
        b_list = parse_range(b_seg)
        c_list = parse_range(c_seg)
        d_list = parse_range(d_seg)
        for a in a_list:
            for b in b_list:
                for c in c_list:
                    for d in d_list:
                        ip_ports.append(f"{a}.{b}.{c}.{d}:{port}")
    else:
        a, b, c, d = map(int, ip_info.split('.'))
        ip_ports = [f"{a}.{b}.{x}.{y}:{port}" for x in range(256) for y in range(1, 256)]
    checked = [0]
    Thread(target=show_progress, daemon=True).start()
    # 并发改成10，GitHub Action专用
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(check_ip_port, ip_port, url_end, stop_event): ip_port
            for ip_port in ip_ports
        }
        for future in as_completed(futures):
            if stop_event.is_set():
                break
            result = future.result()
            if result:
                valid_ip_ports.append(result)
                if max_find is not None and len(valid_ip_ports) >= max_find:
                    print(f"✅ 已收集到{max_find}个有效IP，停止当前网段扫描")
                    stop_event.set()
                    break
            checked[0] += 1
    return valid_ip_ports

def multicast_province(config_file):
    filename = os.path.basename(config_file)
    province, operator = filename.split('_')[:2]
    print(f"{'=' * 25}\n   获取: {province}_{operator}ip_port\n{'=' * 25}")
    configs = read_config(config_file)
    if not configs:
        return
    print(f"读取完成，共需扫描 {len(configs)}组")
    all_ip_ports = []
    for ip_info, port, url_end, mode, max_find in configs:
        if mode == "full":
            print(f"\n开始扫描第一段固定 {ip_info}，范围 {ip_info}.1-100.1-100.1-100:{port}{url_end}")
        elif mode == "range4":
            a_seg, b_seg, c_seg, d_seg = ip_info
            print(f"\n开始扫描四段范围 {a_seg}.{b_seg}.{c_seg}.{d_seg}:{port}{url_end}")
        else:
            print(f"\n开始扫描  http://{ip_info}:{port}{url_end}")
        all_ip_ports.extend(scan_ip_port(ip_info, port, url_end, mode, max_find))

    # ====== 排序逻辑更新：同IP不同端口全部保留，只剔除完全重复的【IP:端口】 ======
    def sort_key(ip_port_str):
        ip_addr, port_num = ip_port_str.split(":")
        nums = list(map(int, ip_addr.split(".")))
        # 先按IP四段数字排序，IP相同再按端口数字从小到大
        return (*nums, int(port_num))

    # 有序去重：只有完全一模一样的ip:port才删除，同IP不同端口全部保留
    seen = set()
    unique_list = []
    for item in all_ip_ports:
        if item not in seen:
            seen.add(item)
            unique_list.append(item)

    all_ip_ports = sorted(unique_list, key=sort_key)
    # ====== 排序逻辑结束 ======

    print(f"\n{province}_{operator} 扫描完成，获取有效ip_port共：{len(all_ip_ports)}个\n{all_ip_ports}\n")
    with open(f"ip/{province}_{operator}_ip.txt", 'w', encoding='utf-8') as f:
        f.write('\n'.join(all_ip_ports) + '\n')
    template_file = os.path.join('template', f"template_{province}_{operator}.txt")
    if not os.path.exists(template_file):
        print(f"缺少模板文件: {template_file}")
        return
    with open(template_file, 'r', encoding='utf-8') as f:
        channels = f.readlines()
    output = []
    for ip in all_ip_ports:
        output.extend([channel.replace("ipipip", f"{ip}") for channel in channels])
    with open(f"组播_{province}_{operator}.txt", 'w', encoding='utf-8') as f:
        f.write(f"{province}_{operator}-组播,#genre#\n")
        for channel in output:
            f.write(channel)

for config_file in glob.glob(os.path.join('ip', '*_config.txt')):
    multicast_province(config_file)
print("获取完成")
