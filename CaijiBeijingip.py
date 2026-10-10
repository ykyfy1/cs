from threading import Thread
import os
import time
import glob
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

# 全局session复用连接，减少socket占用
session = requests.Session()
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50)
session.mount("http://", adapter)

def parse_range(s):
    """解析 [1-100] 这种字符串，返回range对象"""
    s = s.strip().strip("[]")
    if "-" in s:
        start, end = s.split("-")
        return range(int(start), int(end)+1)
    else:
        # 固定数字，例如 [183]
        val = int(s)
        return range(val, val+1)

def read_config(config_file):
    print(f"读取设置文件：{config_file}")
    ip_configs = []
    try:
        with open(config_file, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if "," in line and not line.startswith("#") and line:
                    parts = line.split(',')
                    ip_port_raw = parts[0].strip()
                    prefix_seg_type = parts[1].strip()

                    # 判断是否自定义区间模式，例如 183:[1-100]:[1-100]:[1-100]:8001,custom
                    if prefix_seg_type == "custom":
                        ip_part, port = ip_port_raw.split(':')
                        ip_parts = ip_part.split(':')
                        # ip_parts: ['183','[1-100]','[1-100]','[1-100]']
                        r1 = parse_range(ip_parts[0])
                        r2 = parse_range(ip_parts[1])
                        r3 = parse_range(ip_parts[2])
                        r4 = parse_range(ip_parts[3])
                        ip_configs.append((None, port, "/status", "custom", (r1,r2,r3,r4)))
                        print(f"第{line_num}行：自定义区间模式，{ip_parts} 端口={port}/status 添加到扫描列表")
                    else:
                        # 原有逻辑保留 prefix_seg=1/2/3/4
                        ip_part, port = ip_port_raw.split(':')
                        prefix_seg = int(prefix_seg_type)
                        url_end = "/stat" if prefix_seg == 1 else "/status"
                        ip_configs.append((ip_part, port, url_end, prefix_seg, None))
                        print(f"第{line_num}行：IP={ip_part} 保留前{prefix_seg}段，端口={port}{url_end} 添加到扫描列表")
        return ip_configs
    except Exception as e:
        print(f"读取文件错误: {e}")
        return None

# 发送get请求检测url是否可访问
def check_ip_port(ip_port, url_end):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
    }
    # 第一次探测 status / stat 页面
    try:
        url = f"http://{ip_port}{url_end}"
        resp = session.get(url, timeout=1.2, headers=headers)
        resp.raise_for_status()
        if "Multi stream daemon" in resp.text or "udpxy status" in resp.text:
            print(f"{url} 访问成功")
            return ip_port
    except Exception:
        pass

    # status页面访问失败，尝试udp流探测（通用测试地址）
    try:
        test_stream = f"http://{ip_port}/udp/239.0.0.1:1234"
        resp = session.get(test_stream, timeout=1.2, headers=headers, stream=True)
        if resp.status_code in (200, 201, 206):
            print(f"{ip_port} udp流探测成功（status关闭）")
            return ip_port
    except Exception:
        return None

# 多线程检测url，获取有效ip_port
def scan_ip_port(ip, port, url_end, prefix_seg, custom_range):
    def show_progress():
        while checked[0] < len(ip_ports):
            print(f"已扫描：{checked[0]}/{len(ip_ports)}, 有效ip_port：{len(valid_ip_ports)}个")
            time.sleep(20)
    valid_ip_ports = []
    ip_ports = []
    if custom_range is not None:
        # 自定义区间模式
        r1,r2,r3,r4 = custom_range
        for a in r1:
            for b in r2:
                for c in r3:
                    for d in r4:
                        ip_ports.append(f"{a}.{b}.{c}.{d}:{port}")
    else:
        # 原有prefix_seg逻辑
        a, b, c, d = ip.split('.')
        if prefix_seg == 1:
            # a.*.*.*
            for B in range(0,256):
                for C in range(0,256):
                    for D in range(1,256):
                        ip_ports.append(f"{a}.{B}.{C}.{D}:{port}")
        elif prefix_seg == 2:
            # a.b.*.* 原版逻辑
            for X in range(0,256):
                for Y in range(1,256):
                    ip_ports.append(f"{a}.{b}.{X}.{Y}:{port}")
        elif prefix_seg == 3:
            # a.b.c.*
            for Y in range(1,256):
                ip_ports.append(f"{a}.{b}.{c}.{Y}:{port}")
        elif prefix_seg == 4:
            # 单个IP
            ip_ports.append(f"{a}.{b}.{c}.{d}:{port}")

    checked = [0]
    Thread(target=show_progress, daemon=True).start()
    # 调低并发到30
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(check_ip_port, ip_port, url_end): ip_port for ip_port in ip_ports}
        for future in as_completed(futures):
            result = future.result()
            if result:
                valid_ip_ports.append(result)
            checked[0] += 1
    return valid_ip_ports

def multicast_province(config_file):
    filename = os.path.basename(config_file)
    province, operator = filename.split('_')[:2]
    print(f"{'='*25}\n   获取: {province}_{operator}ip_port\n{'='*25}")
    configs = read_config(config_file)
    if not configs:
        return
    print(f"读取完成，共需扫描 {len(configs)}组")
    all_ip_ports = []
    for ip, port, url_end, prefix_seg, custom_range in configs:
        print(f"\n开始扫描")
        res = scan_ip_port(ip, port, url_end, prefix_seg, custom_range)
        all_ip_ports.extend(res)
    all_ip_ports = sorted(set(all_ip_ports))
    print(f"\n{province}_{operator} 扫描完成，获取有效ip_port共：{len(all_ip_ports)}个\n{all_ip_ports}\n")
    os.makedirs("ip", exist_ok=True)
    with open(f"ip/{province}_{operator}_ip.txt", 'w', encoding='utf-8') as f:
        f.write('\n'.join(all_ip_ports) + '\n')    #有效ip_port写入文件
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
print(f"获取完成")
