from threading import Thread
import os
import time
import glob
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

def parse_range(s):
    """解析范围，支持单值、范围a-b、多值a,b"""
    s = s.strip()
    if ',' in s:
        return [int(x.strip()) for x in s.split(',')]
    if '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end)+1))
    return [int(s)]

def expand_ip(ip_expr):
    """
    解析IP表达式：支持 171,183.100-200.100-200.100-200
    返回所有展开ip列表
    """
    parts = ip_expr.split('.')
    while len(parts) <4:
        parts.append("1")
    a_list = parse_range(parts[0])
    b_list = parse_range(parts[1])
    c_list = parse_range(parts[2])
    d_list = parse_range(parts[3])
    ip_list = []
    for a in a_list:
        for b in b_list:
            for c in c_list:
                for d in d_list:
                    ip_list.append(f"{a}.{b}.{c}.{d}")
    return ip_list

def parse_ports(port_str):
    """解析端口 8001|8002"""
    return [int(p.strip()) for p in port_str.split('|')]

def read_config(config_file):
    print(f"读取设置文件：{config_file}")
    task_list = []
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "," in line:
                    main_part, mode = line.split(',',1)
                    ip_expr, port_str = main_part.split(':')
                    ip_list = expand_ip(ip_expr)
                    ports = parse_ports(port_str)
                    # mode 0=不限制；1=/stat；其他=/status
                    url_end = "/stat" if mode.strip() == "1" else "/status"
                    for ip in ip_list:
                        for p in ports:
                            task_list.append((ip, p, url_end))
                    print(f"第{line_num}行：生成 {len(ip_list)} 个IP，{len(ports)}个端口，任务数：{len(ip_list)*len(ports)}")
    except Exception as e:
        print(f"读取配置出错：{e}")
    return task_list

# ========== 下面是扫描主函数（示例，你可以替换你原有扫描逻辑）==========
def scan(ip, port, url_path):
    try:
        url = f"http://{ip}:{port}{url_path}"
        resp = requests.get(url, timeout=1)
        if resp.status_code == 200:
            print(f"✅ 存活：{ip}:{port}{url_path}")
            return True
    except Exception:
        pass
    return False

def run_scan(config_path):
    tasks = read_config(config_path)
    print(f"\n总共待扫描任务：{len(tasks)}")
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = [executor.submit(scan, ip, port, url) for ip, port, url in tasks]
        for f in as_completed(futures):
            f.result()

if __name__ == "__main__":
    run_scan("config.txt")
