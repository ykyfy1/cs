import os
import glob
import asyncio
import aiohttp
import time
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
MAX_TOTAL_FIND = 1000
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
        return list(range(int(start), int(end)+1))
    else:
        return [int(s)]


# ============ 域名识别函数 ============
def is_valid_ipv4(s):
    pat = r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$'
    if not re.fullmatch(pat, s):
        return False
    parts = s.split(".")
    return all(0 <= int(p) <= 255 for p in parts)

def is_ip_segment_expr(s):
    pat = r'^\d+(\.[\d,-]+)*$'
    return bool(re.fullmatch(pat, s))

def is_domain_str(s):
    if is_valid_ipv4(s):
        return False
    if is_ip_segment_expr(s):
        return False
    for seg in s.split('.'):
        if '-' in seg:
            return False
    domain_pat = r'^[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+$'
    return bool(re.fullmatch(domain_pat, s))
# ==========================================


def read_config(config_file):
    print(f"读取设置文件：{config_file}", flush=True)
    ip_configs = []
    try:
        with open(config_file, 'r', encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                ip_expr, rest = line.split(':', 1)
                rest_parts = [x.strip() for x in rest.split(',')]
                port_str = rest_parts[0].strip()
                url_flag = int(rest_parts[1]) if len(rest_parts)>=2 else 0
                url_end = "/stat" if url_flag == 1 else "/status"
                max_find = int(rest_parts[2]) if (len(rest_parts)>=3 and rest_parts[2].isdigit()) else None
                ip_part = ip_expr
                ports = [p.strip() for p in port_str.split('|') if p.strip()]
                if not ports:
                    print(f"第{line_num}行未识别到有效端口: {line}", flush=True)
                    continue

                for port in ports:
                    if is_domain_str(ip_part):
                        ip_configs.append(
                            (ip_part, port, url_end, "domain", max_find)
                        )
                        print(
                            f"第{line_num}：域名 {ip_part} 端口 {port_str}，"
                            f"模式={url_flag}({url_end})，最多获取 {max_find if max_find else '不限'} 个", flush=True
                        )
                        continue
                    segs = ip_part.split('.')
                    if len(segs) == 4:
                        ip_configs.append(
                            ((segs[0], segs[1], segs[2], segs[3]), port, url_end, "range4", max_find)
                        )
                    elif 1 <= len(segs) <= 3:
                        while len(segs) < 4:
                            segs.append("0-255")
                        ip_configs.append(
                            ((segs[0], segs[1], segs[2], segs[3]), port, url_end, "range4", max_find)
                        )
                    elif len(segs) == 1:
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


async def tcp_check(ip, port, timeout=0.35):
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, int(port)), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def parse_udpxy_status(html_text):
    info = {
        "version": "",
        "max_client": "",
        "curr_client": ""
    }
    ver_match = re.search(r'udpxy\s+(v\..*?)\s*\(Build\s*(\d+)\)', html_text)
    if ver_match:
        info["version"] = f"{ver_match.group(1)} Build {ver_match.group(2)}"
    maxcli_match = re.search(r'max\s+clients\s*:\s*(\d+)', html_text, re.IGNORECASE)
    if maxcli_match:
        info["max_client"] = maxcli_match.group(1)
    curcli_match = re.search(r'curr\s+clients\s*:\s*(\d+)', html_text, re.IGNORECASE)
    if curcli_match:
        info["curr_client"] = curcli_match.group(1)
    return info


async def check_udpxy(ip_port, url_end, session):
    ip, port = ip_port.split(":")
    tcp_ok = await tcp_check(ip, port, timeout=TCP_TIMEOUT)
    if not tcp_ok:
        return None
    if DEBUG_MODE:
        print(f"[DEBUG] TCP连通成功：{ip_port}", flush=True)

    status_ok = False
    status_html = ""
    try:
        status_url = f"http://{ip}:{port}{url_end}"
        async with session.get(status_url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as resp:
            if resp.status == 200:
                status_ok = True
                status_html = await resp.text()
    except Exception:
        status_ok = False

    if status_ok:
        print(f"✅ {ip_port} | TCP正常 (status页面开启)", flush=True)
        status_info = parse_udpxy_status(status_html)
    else:
        print(f"✅ {ip_port} | TCP正常 (status页面关闭)", flush=True)
        status_info = {"version": "", "max_client": "", "curr_client": ""}

    return {
        "ip_port": ip_port,
        "ip": ip,
        "port": port,
        "version": status_info["version"],
        "max_client": status_info["max_client"],
        "current_conn": status_info["curr_client"],
        "geo": ""
    }


def gen_ip_task(ip_info, port, mode):
    if mode == "domain":
        yield f"{ip_info}:{port}"
    elif mode == "full":
        first_seg = ip_info
        for x in range(1,101):
            for y in range(1,101):
                for z in range(1,101):
                    yield f"{first_seg}.{x}.{y}.{z}:{port}"
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
        a,b,c,d = map(int, ip_info.split('.'))
        for x in range(256):
            for y in range(1,256):
                yield f"{a}.{b}.{x}.{y}:{port}"


def count_total_tasks(ip_info, port, mode):
    total = 0
    if mode == "domain":
        total = 1
    elif mode == "full":
        total = 100*100*100
    elif mode == "range4":
        a_seg, b_seg, c_seg, d_seg = ip_info
        a_list = parse_range(a_seg)
        b_list = parse_range(b_seg)
        c_list = parse_range(c_seg)
        d_list = parse_range(d_seg)
        total = len(a_list)*len(b_list)*len(c_list)*len(d_list)
    else:
        total = 256*255
    return total


async def scan_ip_port(ip_info, port, url_end, mode, max_find, worker_num, session, global_stop_event, total_valid_ref, max_global_find):
    valid_list = []
    checked_count = 0
    total_task = count_total_tasks(ip_info, port, mode)
    task_gen = gen_ip_task(ip_info, port, mode)
    stop_flag = False
    bar_len = 20
    start_time = time.time()
    last_print_pct = -1

    task_queue = asyncio.Queue(maxsize=QUEUE_SIZE)

    async def producer():
        nonlocal stop_flag
        for ip_port_item in task_gen:
            if stop_flag or global_stop_event.is_set():
                break
            await task_queue.put(ip_port_item)
        for _ in range(worker_num):
            await task_queue.put(None)

    async def worker():
        nonlocal checked_count, stop_flag, last_print_pct
        while True:
            item = await task_queue.get()
            if item is None:
                task_queue.task_done()
                break
            if stop_flag or global_stop_event.is_set():
                task_queue.task_done()
                continue
            res = await check_udpxy(item, url_end, session)
            checked_count += 1
            pct = (checked_count / total_task) * 100
            current_step = int(pct // PRINT_STEP_PCT) * PRINT_STEP_PCT
            if current_step > last_print_pct:
                last_print_pct = current_step
                ratio = checked_count / total_task
                bc = "\033[91m" if ratio <0.33 else ("\033[93m" if ratio<0.66 else "\033[92m")
                reset = "\033[0m"
                fill = int(bar_len * ratio)
                bar = "█"*fill + " "*(bar_len-fill)
                used = time.time() - start_time
                speed = checked_count / used if used>0 else 0
                remain = (total_task - checked_count)/speed if speed>0 else 0
                eta_str = format_seconds(remain)
                global_text = f"|全局累计:{total_valid_ref[0]}/{max_global_find}" if max_global_find else ""
                print(f"📊 进度: {checked_count}/{total_task} | {pct:.1f}% |本段有效:{len(valid_list)} | [{bc}{bar}{reset}] |速率:{speed:.2f}/s |剩余:{eta_str} {global_text}",flush=True)
            if res:
                valid_list.append(res)
                total_valid_ref[0] += 1
                if max_find is not None and len(valid_list)>=max_find:
                    print(f"\n✅ 本段收集够{max_find}个，停止扫描",flush=True)
                    stop_flag = True
                if max_global_find and total_valid_ref[0]>=max_global_find:
                    print(f"\n🚨 全局收集到达上限，全部任务停止",flush=True)
                    global_stop_event.set()
            task_queue.task_done()

    producer_task = asyncio.create_task(producer())
    workers = [asyncio.create_task(worker()) for _ in range(worker_num)]
    await producer_task
    await asyncio.gather(*workers)

    total_cost = time.time() - start_time
    avg_speed = checked_count / total_cost if total_cost>0 else 0
    print(f"\n本段扫描结束，共检测{checked_count}个，有效{len(valid_list)}，平均速率{avg_speed:.2f}/s",flush=True)
    return valid_list


async def scan_city(config_file, worker_num, session, global_stop_event, total_valid_ref, max_global_find):
    filename = os.path.basename(config_file)
    province, isp = filename.split("_")[0:2]
    print(f"\n==================== 开始采集：{province}_{isp} ====================",flush=True)
    cfg = read_config(config_file)
    if not cfg:
        return []
    print(f"读取配置共 {len(cfg)} 组任务",flush=True)
    all_valid = []
    for ip_info, port, url_end, mode, max_find in cfg:
        if global_stop_event.is_set():
            print("全局停止，跳过剩余任务",flush=True)
            break
        if mode == "domain":
            print(f"\n---- 域名检测：{ip_info}:{port}{url_end}",flush=True)
        elif mode == "range4":
            print(f"\n---- 四段网段扫描 {ip_info[0]}.{ip_info[1]}.{ip_info[2]}.{ip_info[3]}:{port}{url_end}",flush=True)
        elif mode == "full":
            print(f"\n---- 单A段扫描 {ip_info}.x.x.x:{port}{url_end}",flush=True)
        else:
            print(f"\n---- 网段扫描 {ip_info}:{port}{url_end}",flush=True)
        part_res = await scan_ip_port(ip_info, port, url_end, mode, max_find, worker_num, session, global_stop_event, total_valid_ref, max_global_find)
        all_valid.extend(part_res)
    if GEO_ENABLE and len(all_valid) > 0:
        print(f"\n🔍 批量查询归属地，共{len(all_valid)}个节点",flush=True)
        geo_sem = asyncio.Semaphore(GEO_SEMAPHORE)
        geo_session = aiohttp.ClientSession()
        async with geo_session:
            tasks = []
            for item in all_valid:
                async def fill_geo(obj):
                    obj["geo"] = await get_ip_geo(geo_session, obj["ip"], geo_sem)
                tasks.append(asyncio.create_task(fill_geo(item)))
            await asyncio.gather(*tasks, return_exceptions=True)
    def sort_key(x):
        try:
            mc = int(x["max_client"])
        except:
            mc = 0
        if is_domain_str(x["ip"]):
            ip_arr = [999,999,999,999]
        else:
            try:
                ip_arr = list(map(int, x["ip"].split(".")))
            except:
                ip_arr = [999,999,999,999]
        prt = int(x["port"])
        return (-mc, *ip_arr, prt)
    all_valid.sort(key=sort_key)
    os.makedirs("ip", exist_ok=True)
    out_path = f"ip/good_{province}_{isp}_ip.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        for item in all_valid:
            f.write(f"{item['ip_port']},{item['geo']},{item['version']}\n")
    print(f"\n✅ {province}_{isp} 结果已保存至 {out_path}，有效节点：{len(all_valid)}",flush=True)
    return all_valid


async def main():
    worker_num = SEMAPHORE_NUM
    global_stop = asyncio.Event()
    total_valid = [0]
    conn = aiohttp.TCPConnector(ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=conn) as sess:
        cfg_files = glob.glob("ip/*_config.txt")
        print(f"发现配置文件数量：{len(cfg_files)}",flush=True)
        all_result = []
        for f in cfg_files:
            if global_stop.is_set():
                break
            city_res = await scan_city(f, worker_num, sess, global_stop, total_valid, MAX_TOTAL_FIND)
            all_result.extend(city_res)
    print(f"\n==================== 全部采集任务结束 ====================")
    print(f"全局总共有效节点：{len(all_result)}",flush=True)


if __name__ == "__main__":
    asyncio.run(main())
