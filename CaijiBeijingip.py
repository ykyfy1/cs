import os
import glob
import asyncio
import aiohttp
import time
import json
import csv
import re

# ===================== 可配置参数 =====================
SEMAPHORE_NUM = 35      # 本地用35，GA改成22
TCP_TIMEOUT = 0.35      # 本地0.35，GA改成0.4
HTTP_TIMEOUT = 1.0
DEBUG_MODE = False
GEO_SEMAPHORE = 8
GEO_ENABLE = True
PRINT_STEP_PCT = 2
QUEUE_SIZE = SEMAPHORE_NUM * 2
MAX_TOTAL_FIND = 200
# ======================================================
geo_cache = {}


def format_seconds(sec):
    """格式化秒数为 时:分:秒，用于ETA剩余预估时间"""
    sec = max(sec, 0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    if h > 0:
        return f"{h}h{m}m{s:.1f}s"
    elif m > 0:
        return f"{m}m{s:.1f}s"
    else:
        return f"{s:.1f}s"


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
    print(f"读取设置文件：{config_file}", flush=True)
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
                    print(f"第{line_num}行未识别到有效端口: {line}", flush=True)
                    continue

                dot_count = ip_part.count('.')
                for port in ports:
                    # ========= 核心修改：dot_count=1/2/3 全部走range4，不足4段补0-255 =========
                    if 1 <= dot_count <= 3:
                        segs = ip_part.split('.')
                        # 不够4段，后面补上 "0-255"
                        while len(segs) < 4:
                            segs.append("0-255")
                        ip_configs.append(
                            ((segs[0], segs[1], segs[2], segs[3]), port, url_end, "range4", max_find)
                        )
                    elif dot_count == 0:
                        first_seg = ip_part
                        ip_configs.append(
                            (first_seg, port, url_end, "full", max_find)
                        )
                    else:
                        print(f"第{line_num}行IP段格式异常，跳过: {line}", flush=True)
                        continue

                    print(
                        f"第{line_num}：{ip_part} 端口 {port_str}，"
                        f"模式={url_flag}({url_end})，最多获取 {max_find if max_find else '不限'} 个", flush=True
                    )
    except Exception as e:
        print(f"读取文件错误: {e}", flush=True)
        return None
    return ip_configs


async def get_ip_geo(session, ip, sem):
    """异步查询IP归属地，后置执行，带缓存"""
    if ip in geo_cache:
        return geo_cache[ip]
    try:
        async with sem:
            url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data["status"] == "success":
                        loc = f"{data['regionName']} {data['city']} | {data['isp']}"
                        geo_cache[ip] = loc
                        return loc
    except Exception:
        pass
    geo_cache[ip] = "未知归属地"
    return "未知归属地"


async def tcp_check(ip, port, timeout=TCP_TIMEOUT):
    """用可靠的open_connection，GA环境不会误判端口"""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, int(port)), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def parse_udpxy_status(html_text):
    """解析udpxy status页面，提取纯净版本号、最大客户端、当前连接"""
    info = {
        "version": "",
        "max_client": "",
        "current_conn": ""
    }
    ver_match = re.search(r'udpxy\s+(v\.\s*[\d\.]+)\s*\(Build\s*(\d+)\)', html_text)
    if ver_match:
        info["version"] = f"{ver_match.group(1)} Build {ver_match.group(2)}"
    maxcli_match = re.search(r'max clients\s*:\s*(\d+)', html_text, re.IGNORECASE)
    currcli_match = re.search(r'current clients\s*:\s*(\d+)', html_text, re.IGNORECASE)
    if maxcli_match:
        info["max_client"] = maxcli_match.group(1)
    if currcli_match:
        info["current_conn"] = currcli_match.group(1)
    return info


async def check_udpxy(ip_port, url_end, session):
    ip, port = ip_port.split(":")
    tcp_open = await tcp_check(ip, port, timeout=TCP_TIMEOUT)
    if not tcp_open:
        return None
    if DEBUG_MODE:
        print(f"[DEBUG] TCP端口通：{ip_port}", flush=True)
    try:
        url = f"http://{ip_port}{url_end}"
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                text = await resp.text()
                if "Multi stream daemon" in text or "udpxy status" in text:
                    status_info = parse_udpxy_status(text)
                    print(f"✅ {url} | 版本:{status_info['version']} | 最大客户端:{status_info['max_client']} | 当前连接:{status_info['current_conn']}", flush=True)
                    return {
                        "ip_port": ip_port,
                        "ip": ip,
                        "port": port,
                        "version": status_info["version"],
                        "max_client": status_info["max_client"],
                        "current_conn": status_info["current_conn"],
                        "geo": ""
                    }
    except Exception as e:
        if DEBUG_MODE:
            print(f"[DEBUG] HTTP访问异常 {ip_port} : {str(e)}", flush=True)
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


async def scan_ip_port(ip_info, port, url_end, mode, max_find, semaphore, session, global_stop_event, total_valid, max_total_find):
    valid_ip_ports = []
    checked = 0
    total_task = count_total_tasks(ip_info, port, mode)
    task_generator = gen_ip_task(ip_info, port, mode)
    stop_flag = False
    bar_length = 20
    start_time = time.time()
    last_print_pct = -1

    q = asyncio.Queue(maxsize=QUEUE_SIZE)

    # 生产者：不断往队列填充ip
    async def producer():
        nonlocal stop_flag
        for ip_port in task_generator:
            if stop_flag or global_stop_event.is_set():
                break
            await q.put(ip_port)
        # 生产者结束，放哨兵标记
        for _ in range(SEMAPHORE_NUM):
            await q.put(None)

    # 消费者worker
    async def worker():
        nonlocal checked, stop_flag, last_print_pct
        while True:
            ip_port = await q.get()
            if ip_port is None:
                q.task_done()
                break
            if stop_flag or global_stop_event.is_set():
                q.task_done()
                continue
            res = await check_udpxy(ip_port, url_end, session)
            checked +=1
            pct = (checked / total_task)*100
            current_step = int(pct // PRINT_STEP_PCT) * PRINT_STEP_PCT
            # ====== 每1%打印一次进度 ======
            if current_step > last_print_pct:
                last_print_pct = current_step
                fraction = checked / total_task
                # 三色逻辑：0~33%绿，33~66%黄，66~100%红
                if fraction < 0.33:
                    bar_color = "\033[91m" #红色
                elif fraction < 0.66:
                    bar_color = "\033[93m" #黄色
                else:
                    bar_color = "\033[92m" #绿色
                RESET = "\033[0m"
                filled_len = int(bar_length * checked / total_task)
                percent_text = f"{pct:.2f}%"
                # 色块填满整条进度条
                bar_chars = list("█" * filled_len + " " * (bar_length - filled_len))
                # 百分比文字居中叠加在进度条色块上
                text_start = (bar_length - len(percent_text)) // 2
                for i, ch in enumerate(percent_text):
                    if text_start + i < bar_length:
                        bar_chars[text_start + i] = ch
                bar_str = "".join(bar_chars)
                cost = time.time() - start_time
                speed = round(checked / cost, 2)
                remain_count = total_task - checked
                eta_sec = remain_count / speed if speed > 0 else 0
                eta_str = format_seconds(eta_sec)
                total_valid_text = f"| 全局累计:{total_valid[0]}/{max_total_find}" if max_total_find else f"| 全局累计:{total_valid[0]}"
                print(f"📊 进度：{checked}/{total_task} | {pct:.2f}% | 本段存活：{len(valid_ip_ports)} | [{bar_color}{bar_str}{RESET}] | 速度：{speed}个/秒 | ETA:{eta_str} {total_valid_text}", flush=True)
            if res:
                valid_ip_ports.append(res)
                total_valid[0] += 1
                # 单段上限判断
                if max_find is not None and len(valid_ip_ports) >= max_find:
                    print(f"\n✅ 已收集到{max_find}个有效IP，停止当前网段扫描", flush=True)
                    stop_flag = True
                # 全局上限判断
                if max_total_find is not None and total_valid[0] >= max_total_find:
                    print(f"\n🚨 已达到全局最大有效数{max_total_find}，停止全部剩余扫描任务", flush=True)
                    global_stop_event.set()
            q.task_done()

    # 启动生产者 + N个worker
    producer_task = asyncio.create_task(producer())
    workers = [asyncio.create_task(worker()) for _ in range(SEMAPHORE_NUM)]
    await producer_task
    await asyncio.gather(*workers)

    total_cost = time.time() - start_time
    avg_speed = round(checked / total_cost,2)
    print(f"\n本段扫描结束，总共检测 {checked} 个，有效数量：{len(valid_ip_ports)}，平均速度 {avg_speed}个/秒", flush=True)
    return valid_ip_ports


async def multicast_province(config_file, semaphore, session, global_stop_event, total_valid, max_total_find):
    filename = os.path.basename(config_file)
    province, operator = filename.split('_')[:2]
    print(f"{'=' * 25}\n   获取: {province}_{operator}ip_port\n{'=' * 25}", flush=True)
    configs = read_config(config_file)
    if not configs:
        return []
    print(f"读取完成，共需扫描 {len(configs)}组", flush=True)
    all_result = []
    for ip_info, port, url_end, mode, max_find in configs:
        if global_stop_event.is_set():
            print("全局停止，跳过该省份剩余网段", flush=True)
            break
        if mode == "full":
            print(f"\n开始扫描第一段固定 {ip_info}，范围 {ip_info}.1-100.1-100.1-100:{port}{url_end}", flush=True)
        elif mode == "range4":
            a_seg, b_seg, c_seg, d_seg = ip_info
            print(f"\n开始扫描四段范围 {a_seg}.{b_seg}.{c_seg}.{d_seg}:{port}{url_end}", flush=True)
        else:
            print(f"\n开始扫描  http://{ip_info}:{port}{url_end}", flush=True)
        result = await scan_ip_port(ip_info, port, url_end, mode, max_find, semaphore, session, global_stop_event, total_valid, max_total_find)
        all_result.extend(result)
    # ====================== 扫描全部完成后，批量查询归属地 ======================
    if GEO_ENABLE and len(all_result) > 0:
        print(f"\n🔍 开始批量查询{len(all_result)}个IP的归属地...", flush=True)
        geo_sem = asyncio.Semaphore(GEO_SEMAPHORE)
        async with aiohttp.ClientSession() as geo_session:
            tasks = []
            for item in all_result:
                async def fill_geo(it):
                    try:
                        it["geo"] = await get_ip_geo(geo_session, it["ip"], geo_sem)
                    except Exception:
                        it["geo"] = "未知归属地"
                tasks.append(fill_geo(item))
            await asyncio.gather(*tasks, return_exceptions=True)
    else:
        print("\nℹ️ 归属地查询已关闭，全部填充为【未知归属地】", flush=True)
        for item in all_result:
            item["geo"] = "未知归属地"
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
    # ========== 输出多类型文件 ==========
    with open(f"ip/good_{province}_{operator}_ip.txt", 'w', encoding='utf-8') as f:
        for item in all_result:
            f.write(f"{item['ip_port']}\n")
    with open(f"ip/good_{province}_{operator}_full.txt", 'w', encoding='utf-8') as f:
        for item in all_result:
            f.write(f"{item['ip_port']} | 版本:{item['version']} | 最大客户端:{item['max_client']} | 当前连接:{item['current_conn']} | {item['geo']}\n")
    csv_path = f"ip/good_{province}_{operator}_result.csv"
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=["ip_port","ip","port","version","max_client","current_conn","geo"])
        writer.writeheader()
        writer.writerows(all_result)
    json_path = f"ip/good_{province}_{operator}_result.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_result, f, ensure_ascii=False, indent=2)
    template_file = os.path.join('template', f"template_{province}_{operator}.txt")
    if os.path.exists(template_file):
        with open(template_file, 'r', encoding='utf-8') as f:
            template_content = f.read()
        output = []
        for item in all_result:
            output.append(template_content.replace("ipipip", f"{item['ip_port']}"))
        with open(f"组播_{province}_{operator}.txt", 'w', encoding='utf-8') as f:
            f.write('\n'.join(output))
    print(f"\n{province}_{operator} 扫描完成，获取有效udpxy共：{len(all_result)}个", flush=True)
    print(f" -> 测速用txt：ip/good_{province}_{operator}_ip.txt （纯ip:port，测速程序读取）", flush=True)
    print(f" -> 带归属地完整txt：ip/good_{province}_{operator}_full.txt", flush=True)
    print(f" -> 表格csv：{csv_path}", flush=True)
    print(f" -> json详情：{json_path}", flush=True)
    print(f" -> IPTV组播文件：组播_{province}_{operator}.txt", flush=True)
    return all_result


async def main():
    semaphore = asyncio.Semaphore(SEMAPHORE_NUM)
    global_stop_event = asyncio.Event()
    total_valid = [0]
    # 全局复用http连接池 + DNS缓存优化，减少重复DNS查询，小幅提速
    connector = aiohttp.TCPConnector(limit=SEMAPHORE_NUM, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        config_files = glob.glob(os.path.join('ip', '*_config.txt'))
        for cfg in config_files:
            if global_stop_event.is_set():
                print("全局停止，跳过剩余省份配置文件", flush=True)
                break
            await multicast_province(cfg, semaphore, session, global_stop_event, total_valid, MAX_TOTAL_FIND)
    print(f"✅ 全部扫描任务完成，总共累计收集有效源 {total_valid[0]} 个", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
