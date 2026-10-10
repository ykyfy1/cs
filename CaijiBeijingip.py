from threading import Thread
import os
import time
import glob
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

def read_config(config_file):
    print(f"读取设置文件：{config_file}")
    ip_configs = []
    try:
        with open(config_file, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "," in line:
                    parts = line.split(',')
                    ip_part, port = parts[0].strip().split(':')
                    url_end = "/stat" if int(parts[1]) == 1 else "/status"
                    # 判断是否只有第一段（如 183:8001,12）
                    dot_count = ip_part.count('.')
                    if dot_count == 0:
                        # 只有第一段，后面三段全部范围1~100
                        first_seg = ip_part
                        ip_configs.append((first_seg, port, url_end, "full"))
                        print(f"第{line_num}行：固定第一段{first_seg}，扫描 {first_seg}.1-100.1-100.1-100:{port}  添加到扫描列表")
                    else:
                        # 原有逻辑：正常ip四段写法
                        a, b, c, d = ip_part.split('.')
                        ip = f"{a}.{b}.1.1"
                        ip_configs.append((ip, port, url_end, "old"))
                        print(f"第{line_num}行：http://{ip}:{port}{url_end} 添加到扫描列表")
        return ip_configs
    except Exception as e:
        print(f"读取文件错误: {e}")
        return None

# 发送get请求检测url是否可访问
def check_ip_port(ip_port, url_end):
    try:
        url = f"http://{ip_port}{url_end}"
        resp = requests.get(url, timeout=2)
        resp.raise_for_status()
        if "Multi stream daemon" in resp.text or "udpxy status" in resp.text:
            print(f"{url} 访问成功")
            return ip_port
    except Exception:
        return None

# 多线程检测url，获取有效ip_port
def scan_ip_port(ip_info, port, url_end, mode):
    def show_progress():
        while checked[0] < len(ip_ports):
            print(f"已扫描：{checked[0]}/{len(ip_ports)}, 有效ip_port：{len(valid_ip_ports)}个")
            time.sleep(20)
    valid_ip_ports = []
    ip_ports = []
    if mode == "full":
        # 新模式：固定第一段ip_info，第2、3、4段 1~100
        first = ip_info
        for x in range(1, 101):
            for y in range(1, 101):
                for z in range(1, 101):
                    ip_ports.append(f"{first}.{x}.{y}.{z}:{port}")
    else:
        # 旧模式，保留原有逻辑
        a, b, c, d = map(int, ip_info.split('.'))
        ip_ports = [f"{a}.{b}.{x}.{y}:{port}" for x in range(256) for y in range(1,256)]

    checked = [0]
    Thread(target=show_progress, daemon=True).start()
    with ThreadPoolExecutor(max_workers=300) as executor:
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
    for ip_info, port, url_end, mode in configs:
        if mode == "full":
            print(f"\n开始扫描第一段固定 {ip_info}，范围 {ip_info}.1-100.1-100.1-100:{port}{url_end}")
        else:
            print(f"\n开始扫描  http://{ip_info}:{port}{url_end}")
        all_ip_ports.extend(scan_ip_port(ip_info, port, url_end, mode))
    all_ip_ports = sorted(set(all_ip_ports))
    print(f"\n{province}_{operator} 扫描完成，获取有效ip_port共：{len(all_ip_ports)}个\n{all_ip_ports}\n")
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
