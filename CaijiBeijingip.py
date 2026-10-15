import os
import glob
import asyncio
import aiohttp
import socket

def parse_range(s):
    """支持 单值、171-183连续范围、171,183逗号多选"""
    s = s.strip()
    if ',' in s:
        res = []
        for part in s.split(','):
            part = part.strip()
            if '-' in part:
                st, ed = part.split('-')
                res.extend(range(int(st), int(ed)+1))
            else:
                res.append(int(part))
        return res
    if '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end) + 1))
    else:
        return [int(s)]

def read_config(config_file):
    print(f"读取设置文件：{config_file}")
    ip_configs = []
    try:
        with open(config_file, 'r', encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 只拆分第一个冒号，区分【IP表达式】和【端口+参数】
                ip_expr, rest = line.split(':', 1)
                # rest 样例：4022|8888|8188,0,10
                rest_parts = rest.split(',')
                port_str = rest_parts[0].strip()
                url_flag = int(rest_parts[1])
                url_end = "/stat" if url_flag == 1 else "/status"
                max_find = int(rest_parts[2]) if len(rest_parts) >= 3 else None
                ip_part = ip_expr
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
                        a, b, c, d = map(int, ip_part.split('.'))
                        ip = f"{a}.{b}.1.1"
                        ip_configs.append(
                            (ip, port, url_end, "old", max_find)
                        )
                print(
                    f"第{line_num}：{ip_part} 端口 {port_str}，"
                    f"模式={url_flag}({url_end})，最多获取 {max_find if max_find else '不限'} 个"
                )
        return ip_configs
    except Exception as e:
        print(f"读取文件错误: {e}")
        return None

async def tcp_check(ip, port, timeout=1):
    """异步TCP端口预检"""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, int(port)), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False

async def check_udpxy(ip_port, url_end):
    ip, port = ip_port.split(":")
    if not await tcp_check(ip, port, timeout=1):
        return None
    try:
        url = f"http://{ip_port}{url_end}"
        timeout = aiohttp.ClientTimeout(total=2)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if "Multi stream daemon" in text or "udpxy status" in text:
                        print(f"✅ {url} 访问成功")
                        return ip_port
    except Exception:
        pass
    return None

def gen_ip_task(ip_info, port, mode):
    """生成器懒加载IP:port，不一次性全部载入内存"""
    if mode == "full":
        first = ip_info
        for x in range(1, 101):
            for y in range(1, 101):
                for z in range(1, 101):
                    yield f"{first}.{x}.{y}.{z}:{port}"
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
                        yield f"{a}.{b}.{c}.{d}:{port}"
    else:
        a, b, c, d = map(int, ip_info.split('.'))
        for x in range(256):
            for y in range(1, 256):
                yield f"{a}.{b}.{x}.{y}:{port}"

def count_total_tasks(ip_info, port, mode):
    """预计算总任务数量，用于进度百分比"""
    total = 0
    if mode == "full":
        total = 100 * 100 * 100
    elif mode == "range4":
        a_seg, b_seg, c_seg, d_seg = ip_info
        a_list = parse_range(a_seg)
        b_list = parse_range(b_seg)
        c_list = parse_range(c_seg)
        d_list = parse_range(d_seg)
        total = len(a_list)*len(b_list)*len(c_list)*len(d_list)
    else:
        total = 256 * 255
    return total

async def scan_ip_port(ip_info, port, url_end, mode, max_find, semaphore):
    valid_ip_ports = []
    checked = 0
    total_task = count_total_tasks(ip_info, port, mode)
    task_generator = gen_ip_task(ip_info, port, mode)
    stop_flag = False
    print_interval = 50  # 每扫描50条打印一次进度，控制日志行数，适配Github Action

    async def task_wrapper(ip_port):
        nonlocal checked, stop_flag
        if stop_flag:
            return
        res = await check_udpxy(ip_port, url_end)
        checked += 1
        # 间隔打印进度，不再刷屏
        if checked % print_interval == 0:
            pct = (checked / total_task)*100
            print(f"📊 进度：{checked}/{total_task} | {pct:.2f}% | 存活：{len(valid_ip_ports)}")
        if res:
            valid_ip_ports.append(res)
            if max_find is not None and len(valid_ip_ports) >= max_find:
                print(f"\n✅ 已收集到{max_find}个有效IP，停止当前网段扫描")
                stop_flag = True

    # 修复核心：逐个迭代生成任务，受信号量控制，不会一次性创建全部task
    tasks = set()
    for ip_port in task_generator:
        if stop_flag:
            break
        # 等待有空位
        if len(tasks) >= semaphore._value:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for f in done:
                await f
        # 新建任务
        t = asyncio.create_task(task_wrapper(ip_port))
        tasks.add(t)
    # 等待剩余任务完成
    await asyncio.wait(tasks)
    print(f"\n本段扫描结束，总共检测 {checked} 个，有效数量：{len(valid_ip_ports)}")
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
    # GitHub Action 并发固定10，不要调大！Action公网网络很弱
    sem_num = 10
    for ip_info, port, url_end, mode, max_find in configs:
        if mode == "full":
            print(f"\n开始扫描第一段固定 {ip_info}，范围 {ip_info}.1-100.1-100.1-100:{port}{url_end}")
        elif mode == "range4":
            a_seg, b_seg, c_seg, d_seg = ip_info
            print(f"\n开始扫描四段范围 {a_seg}.{b_seg}.{c_seg}.{d_seg}:{port}{url_end}")
        else:
            print(f"\n开始扫描  http://{ip_info}:{port}{url_end}")
        # 执行异步扫描
        result = asyncio.run(scan_ip_port(ip_info, port, url_end, mode, max_find, asyncio.Semaphore(sem_num)))
        all_ip_ports.extend(result)

    # ====== 排序逻辑：同IP不同端口全部保留，只剔除完全重复IP:port ======
    def sort_key(ip_port_str):
        ip_addr, port_num = ip_port_str.split(":")
        nums = list(map(int, ip_addr.split(".")))
        return (*nums, int(port_num))
    seen = set()
    unique_list = []
    for item in all_ip_ports:
        if item not in seen:
            seen.add(item)
            unique_list.append(item)
    all_ip_ports = sorted(unique_list, key=sort_key)
    # ====== 排序结束 ======
    print(f"\n{province}_{operator} 扫描完成，获取有效ip_port共：{len(all_ip_ports)}个\n")
    os.makedirs("ip", exist_ok=True)
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

if __name__ == "__main__":
    for config_file in glob.glob(os.path.join('ip', '*_config.txt')):
        multicast_province(config_file)
    print("✅ 全部获取完成")
