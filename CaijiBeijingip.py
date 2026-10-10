from threading import Thread
import os
import time
import datetime
import glob
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# 全局session，防止连接堆积卡死
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
session.mount('http://', adapter)

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
                    ip_part, port_str = ip_port_raw.split(':')
                    # |分割端口列表，【不拆分成多条记录】，直接保存端口列表
                    port_list = [p.strip() for p in port_str.split("|")]
                    option = int(parts[1])
                    url_end = "/status" if option >= 10 else "/stat"
                    # 一条记录保存ip+端口数组，不再循环append多条
                    ip_configs.append((ip_part, port_list, option, url_end))
                    print(f"第{line_num}行：IP={ip_part}，端口列表={port_list} 添加到扫描任务")
        return ip_configs
    except Exception as e:
        print(f"读取文件错误: {e}")


def generate_ip_ports(ip, port_list, option):
    if option == 99:
        # 固定IP模式，返回 [ip:port1, ip:port2, ip:port3...]
        return [f"{ip}:{p}" for p in port_list]
    else:
        # 原有网段模式逻辑不变
        a, b, c, d = ip.split('.')
        if option == 2 or option == 12:
            c_extent = c.split('-')
            c_first = int(c_extent[0]) if len(c_extent) == 2 else int(c)
            c_last = int(c_extent[1]) + 1 if len(c_extent) == 2 else int(c) + 8
            res = []
            for x in range(c_first, c_last):
                for y in range(1, 256):
                    for p in port_list:
                        res.append(f"{a}.{b}.{x}.{y}:{p}")
            return res
        elif option == 0 or option == 10:
            res = []
            for y in range(1, 256):
                for p in port_list:
                    res.append(f"{a}.{b}.{c}.{y}:{p}")
            return res
        else:
            res = []
            for x in range(0,256):
                for y in range(1,256):
                    for p in port_list:
                        res.append(f"{a}.{b}.{x}.{y}:{p}")
            return res


# 发送get请求检测url是否为udpxy
def check_ip_port(ip_port, url_end):
    try:
        url = f"http://{ip_port}{url_end}"
        resp = session.get(url, timeout=(2,2))
        resp.raise_for_status()
        if "Multi stream daemon" in resp.text or "udpxy status" in resp.text:
            print(f"✅ {url} 访问成功")
            return ip_port
    except Exception as e:
        print(f"❌ {ip_port} 失败: {str(e)}")
        return None


# 批量扫描端口列表，同一个线程池一次性跑全部端口
def scan_ip_port(ip, port_list, option, url_end):
    def show_progress():
        while checked[0] < len(ip_ports):
            print(f"已扫描：{checked[0]}/{len(ip_ports)}, 有效ip_port：{len(valid_ip_ports)}个")
            time.sleep(3)

    valid_ip_ports = []
    ip_ports = generate_ip_ports(ip, port_list, option)
    checked = [0]
    Thread(target=show_progress, daemon=True).start()

    if option == 99:
        worker_num = 7
    else:
        worker_num = 300 if option % 2 ==1 else 100

    with ThreadPoolExecutor(max_workers=worker_num) as executor:
        futures = {executor.submit(check_ip_port, item, url_end): item for item in ip_ports}
        for future in as_completed(futures):
            result = future.result()
            if result:
                valid_ip_ports.append(result)
            checked[0] +=1
    return valid_ip_ports


def multicast_province(config_file):
    filename = os.path.basename(config_file)
    province = filename.split('_')[0]
    print(f"\n=========================\n   获取: {province} ip_port\n=========================")
    configs = read_config(config_file)
    print(f"读取完成，共需扫描 {len(configs)}组任务")
    all_ip_ports = []
    for ip, port_list, option, url_end in configs:
        print(f"\n开始批量扫描 IP: {ip}，端口列表：{port_list}")
        all_ip_ports.extend(scan_ip_port(ip, port_list, option, url_end))
    if len(all_ip_ports) !=0:
        all_ip_ports = sorted(set(all_ip_ports))
        print(f"\n{province} 扫描完成，找到有效udpxy共：{len(all_ip_ports)}个\n{all_ip_ports}")
        with open(f"ip/{province}_ip.txt", 'w', encoding='utf-8') as f:
            f.write('\n'.join(all_ip_ports))
        if os.path.exists(f"ip/存档_{province}_ip.txt"):
            with open(f"ip/存档_{province}_ip.txt", 'r', encoding='utf-8') as f:
                lines = f.readlines()
            for ip_port in all_ip_ports:
                ip, port = ip_port.split(":")
                a,b,c,d = ip.split(".")
                lines.append(f"{a}.{b}.{c}.1:{port}\n")
            lines = sorted(set(lines))
            with open(f"ip/存档_{province}_ip.txt", 'w', encoding='utf-8') as f:
                f.writelines(lines)
        template_file = os.path.join('template', f"template_{province}.txt")
        if os.path.exists(template_file):
            with open(template_file, 'r', encoding='utf-8') as f:
                template_content = f.read()
            output = []
            with open(f"ip/{province}_ip.txt", 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    ip_port = line.strip()
                    output.append(f"{province}-组播{line_num},#genre#\n")
                    output.append(template_content.replace("ipipip", ip_port))
            with open(f"组播_{province}.txt", 'w', encoding='utf-8') as f:
                f.writelines(output)
        else:
            print(f"缺少模板文件: {template_file}")
    else:
        print(f"\n{province} 扫描完成，未找到有效udpxy")


def txt_to_m3u(input_file, output_file):
    with open(input_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    with open(output_file, 'w', encoding='utf-8') as f:
        genre = ''
        for line in lines:
            line = line.strip()
            if "," in line:
                channel_name, channel_url = line.split(',',1)
                if channel_url == '#genre#':
                    genre = channel_name
                else:
                    f.write(f'#EXTINF:-1 group-title="{genre}",{channel_name}\n')
                    f.write(f'{channel_url}\n')


def main():
    for config_file in glob.glob(os.path.join('ip', '*_config.txt')):
        multicast_province(config_file)
    file_contents = []
    for file_path in glob.glob('组播_*.txt'):
        with open(file_path, 'r', encoding="utf-8") as f:
            content = f.read()
            file_contents.append(content)
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    current_time = now.strftime("%Y-%m-%d %H:%M")
    with open("zubo_all.txt", "w", encoding="utf-8") as f:
        f.write(f"{current_time}更新,#genre#\n")
        f.write(f"浙江卫视,http://xxx\n")
        f.write('\n'.join(file_contents))
    txt_to_m3u("zubo_all.txt", "zubo_all.m3u")
    print(f"组播生成完成")


if __name__ == "__main__":
    main()
