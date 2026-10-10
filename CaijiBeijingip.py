import asyncio
import aiohttp
import os
import glob
import datetime

# 全局并发限制
SEMAPHORE_LIMIT = 200
TIMEOUT = aiohttp.ClientTimeout(total=1.2)

def read_config(config_file):
    print(f"读取设置文件：{config_file}")
    ip_configs = []
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if "," in line and not line.startswith("#") and line:
                    parts = line.split(',')
                    ip_port_raw = parts[0].strip()
                    ip_part, port_str = ip_port_raw.split(':')
                    port_list = [p.strip() for p in port_str.split("|")]
                    option = int(parts[1])
                    url_end = "/status" if option >= 10 else "/stat"
                    ip_configs.append((ip_part, port_list, option, url_end))
                    print(f"第{line_num}行：IP={ip_part}，端口列表={port_list} 添加到扫描任务")
        return ip_configs
    except Exception as e:
        print(f"读取文件错误: {e}")
        return []


def generate_ip_ports(ip, port_list, option):
    if option == 99:
        return [f"{ip}:{p}" for p in port_list]
    else:
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


async def check_ip_port(session, sem, ip_port, url_end):
    async with sem:
        try:
            url = f"http://{ip_port}{url_end}"
            async with session.get(url, timeout=TIMEOUT) as resp:
                text = await resp.text()
                if "Multi stream daemon" in text or "udpxy status" in text:
                    print(f"✅ {url} 访问成功")
                    return ip_port
        except Exception:
            return None


async def scan_ip_port(ip, port_list, option, url_end):
    valid_ip_ports = []
    ip_ports = generate_ip_ports(ip, port_list, option)
    total = len(ip_ports)
    if option == 99:
        print(f"【固定IP模式，串行扫描端口】")
        for idx, item in enumerate(ip_ports):
            print(f"\n进度：{idx+1}/{total}，正在扫描 {item}")
            # 单任务探测
            connector = aiohttp.TCPConnector(limit=1)
            async with aiohttp.ClientSession(connector=connector, timeout=TIMEOUT) as session:
                res = await check_ip_port(session, asyncio.Semaphore(1), item, url_end)
                if res:
                    valid_ip_ports.append(res)
        return valid_ip_ports
    else:
        # 网段异步并发高速扫描
        print(f"【网段异步高速扫描，总任务 {total}】")
        sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
        connector = aiohttp.TCPConnector(limit=SEMAPHORE_LIMIT, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector, timeout=TIMEOUT) as session:
            tasks = []
            for item in ip_ports:
                task = asyncio.create_task(check_ip_port(session, sem, item, url_end))
                tasks.append(task)
            # 分批收集结果，实时打印进度
            done_count = 0
            for task in asyncio.as_completed(tasks):
                result = await task
                done_count += 1
                if result:
                    valid_ip_ports.append(result)
                    print(f"✅ {result}{url_end} 访问成功")
                print(f"已扫描：{done_count}/{total}, 有效ip_port：{len(valid_ip_ports)}个")
        return valid_ip_ports


async def multicast_province(config_file):
    filename = os.path.basename(config_file)
    province = filename.split('_')[0]
    print(f"\n=========================\n   获取: {province} ip_port\n=========================")
    configs = read_config(config_file)
    print(f"读取完成，共需扫描 {len(configs)}组任务")
    all_ip_ports = []
    for ip, port_list, option, url_end in configs:
        print(f"\n开始扫描 IP: {ip}，端口列表：{port_list}")
        res_list = await scan_ip_port(ip, port_list, option, url_end)
        all_ip_ports.extend(res_list)
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


async def main():
    for config_file in glob.glob(os.path.join('ip', '*_config.txt')):
        await multicast_province(config_file)
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
    asyncio.run(main())
