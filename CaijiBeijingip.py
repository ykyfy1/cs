import os
import glob
import asyncio
import aiohttp
import socket
import time
import json
import csv
# ===================== 可配置参数 =====================
SEMAPHORE_NUM = 20      # GitHub Action建议15~30；本地电脑可50~100
TCP_TIMEOUT = 0.6       # TCP端口预检超时
HTTP_TIMEOUT = 1.2      # udpxy http状态页超时
PRINT_INTERVAL = 10     # 每10条打印一次进度，小范围测试方便观察
# ======================================================
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
                # rest样例：4022|8888|8188,0,10
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
    except Exception as e:
        print(f"读取文件错误: {e}")
        return None
    return ip_configs
async def tcp_check(ip, port, timeout=TCP_TIMEOUT):
    """异步TCP端口预检"""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, int(port)), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False
def parse_udpxy_status(html_text):
    """解析udpxy status页面，提取信息"""
    info = {
        "version": "",
        "max_client": "",
        "current_conn": ""
    }
    lines = html_text.splitlines()
    for line in lines:
        line = line.strip()
        if "udpxy" in line.lower():
            info["version"] = line
        if "Max clients" in line:
            info["max_client"] = line.split(":")[-1].strip()
        if "Current clients" in line:
            info["current_conn"] = line.split(":")[-1].strip()
    return info
async def check_udpxy(ip_port, url_end):
    ip, port = ip_port.split(":")
    if not await tcp_check(ip, port, timeout=TCP_TIMEOUT):
        return None
    try:
        url = f"http://{ip_port}{url_end}"
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if "Multi stream daemon" in text or "udpxy status" in text:
                        status_info = parse_udpxy_status(text)
                        print(f"✅ {url} | 版本:{status_info['version']} | 最大客户端:{status_info['max_client']} | 当前连接:{status_info['current_conn']}")
                        return {
                            "ip_port": ip_port,
                            "ip": ip,
                            "port": port,
                            "version": status_info["version"],
                            "max_client": status_info["max_client"],
                            "current_conn": status_info["current_conn"]
                        }
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
    bar_length = 20
    start_time = time.time()
    async def task_wrapper(ip_port):
        nonlocal checked, stop_flag
        if stop_flag:
            return
        res = await check_udpxy(ip_port, url_end)
        checked += 1
        if checked % PRINT_INTERVAL == 0:
            pct = (checked / total_task)*100
            filled = int(bar_length * checked // total_task)
            bar = '=' * filled + '-' * (bar_length - filled)
            cost = time.time() - start_time
            speed = round(checked / cost, 2)
            print(f"📊 进度：{checked}/{total_task} | {pct:.2f}% | 存活：{len(valid_ip_ports)} | [{bar}] | 速度：{speed}个/秒")
        if res:
            valid_ip_ports.append(res)
            if max_find is not None and len(valid_ip_ports) >= max_find:
                print(f"\n✅ 已收集到{max_find}个有效IP，停止当前网段扫描")
                stop_flag = True
    tasks = set()
    for ip_port in task_generator:
        if stop_flag:
            break
        # 信号量限流
        async with semaphore:
            t = asyncio.create_task(task_wrapper(ip_port))
            tasks.add(t)
            # 清理完成任务
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    # 等待剩余任务结束
    await asyncio.wait(tasks)
    total_cost = time.time() - start_time
    avg_speed = round(checked / total_cost,2)
    print(f"\n本段扫描结束，总共检测 {checked} 个，有效数量：{len(valid_ip_ports)}，平均速度 {avg_speed}个/秒")
    return valid_ip_ports
async def multicast_province(config_file, semaphore):
    filename = os.path.basename(config_file)
    province, operator = filename.split('_')[:2]
    print(f"{'=' * 25}\n   获取: {province}_{operator}ip_port\n{'=' * 25}")
    configs = read_config(config_file)
    if not configs:
        return []
    print(f"读取完成，共需扫描 {len(configs)}组")
    all_result = []
    for ip_info, port, url_end, mode, max_find in configs:
        if mode == "full":
            print(f"\n开始扫描第一段固定 {ip_info}，范围 {ip_info}.1-100.1-100.1-100:{port}{url_end}")
        elif mode == "range4":
            a_seg, b_seg, c_seg, d_seg = ip_info
            print(f"\n开始扫描四段范围 {a_seg}.{b_seg}.{c_seg}.{d_seg}:{port}{url_end}")
        else:
            print(f"\n开始扫描  http://{ip_info}:{port}{url_end}")
        result = await scan_ip_port(ip_info, port, url_end, mode, max_find, semaphore)
        all_result.extend(result)
    # ==========排序逻辑：max_client降序，IP升序，端口升序==========
    def sort_key(item):
        try:
            max_c = int(item["max_client"])
        except ValueError:
            max_c = 0
        ip_nums = list(map(int, item["ip"].split(".")))
        port_num = int(item["port"])
        return (-max_c, *ip_nums, port_num)
    all_result = sorted(all_result, key=sort_key)
    os.makedirs("ip", exist_ok=True)
    # 1.简单txt
    with open(f"ip/{province}_{operator}_ip.txt", 'w', encoding='utf-8') as f:
        for item in all_result:
            f.write(f"{item['ip_port']}\n")
    # 2.csv
    csv_path = f"ip/{province}_{operator}_result.csv"
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=["ip_port","ip","port","version","max_client","current_conn"])
        writer.writeheader()
        writer.writerows(all_result)
    #3.json
    json_path = f"ip/{province}_{operator}_result.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_result, f, ensure_ascii=False, indent=2)
    #4.组播模板
    template_file = os.path.join('template', f"template_{province}_{operator}.txt")
    if os.path.exists(template_file):
        with open(template_file, 'r', encoding='utf-8') as f:
            template_content = f.read()
        output = []
        for item in all_result:
            output.append(template_content.replace("ipipip", f"{item['ip_port']}"))
        with open(f"组播_{province}_{operator}.txt", 'w', encoding='utf-8') as f:
            f.write('\n'.join(output))
    print(f"\n{province}_{operator} 扫描完成，获取有效udpxy共：{len(all_result)}个")
    print(f" -> ip列表txt：ip/{province}_{operator}_ip.txt")
    print(f" -> 表格csv：{csv_path}")
    print(f" -> json详情：{json_path}")
    print(f" -> IPTV组播文件：组播_{province}_{operator}.txt")
    return all_result
async def main():
    semaphore = asyncio.Semaphore(SEMAPHORE_NUM)
    config_files = glob.glob(os.path.join('ip', '*_config.txt'))
    for cfg in config_files:
        await multicast_province(cfg, semaphore)
    print("✅ 全部扫描任务完成")
if __name__ == "__main__":
    asyncio.run(main())
