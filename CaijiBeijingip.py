import os
import glob
import asyncio
import aiohttp
import time
import json
import csv
import re
# ===================== 可配置参数 =====================
SEMAPHORE_NUM = 20      # GitHub Action建议15~30；本地电脑可50~100
TCP_TIMEOUT = 0.4       # TCP端口预检超时，越小扫描越快
HTTP_TIMEOUT = 1.0      # udpxy http状态页超时
DEBUG_MODE = False       # True=开启调试打印；False=关闭调试信息
GEO_SEMAPHORE = 10      # 归属地查询并发，不要太高，防止ip-api限流
GEO_ENABLE = True        # True开启归属地；False跳过归属地查询（不访问外网，稳定）
# 全局总有效数上限：所有省份/网段累计收集到这么多就直接结束全部扫描
# 想不限制就改成 None
MAX_TOTAL_FIND = 200
# ======================================================
# 归属地缓存，同一个IP只查一次
geo_cache = {}
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
                    if dot_count == 3:
                        segs = ip_part.split('.')
                        a_seg, b_seg, c_seg, d_seg = segs
                        ip_configs.append(
                            ((a_seg, b_seg, c_seg, d_seg), port, url_end, "range4", max_find)
                        )
                    elif dot_count == 0:
                        first_seg = ip_info
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
    """异步TCP端口预检"""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, int(port)), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False
def parse_udpxy_status(html_text):
    """解析udpxy status页面，提取纯净版本号、最大客户端、当前连接，自动忽略HTML标签"""
    info = {
        "version": "",
        "max_client": "",
        "current_conn": ""
    }
    # 提取版本号，兼容 udpxy v. 1.0 (Build 25) 这种格式
    ver_match = re.search(r'udpxy\s+(v\.\s*[\d\.]+)\s*\(Build\s*(\d+)\)', html_text)
    if ver_match:
        info["version"] = f"{ver_match.group(1)} Build {ver_match.group(2)}"
    # 不区分大小写，匹配英文
    maxcli_match = re.search(r'max clients\s*:\s*(\d+)', html_text, re.IGNORECASE)
    currcli_match = re.search(r'current clients\s*:\s*(\d+)', html_text, re.IGNORECASE)
    if maxcli_match:
        info["max_client"] = maxcli_match.group(1)
    if currcli_match:
        info["current_conn"] = currcli_match.group(1)
    return info
async def check_udpxy(ip_port, url_end):
    ip, port = ip_port.split(":")
    tcp_open = await tcp_check(ip, port, timeout=TCP_TIMEOUT)
    if not tcp_open:
        return None
    if DEBUG_MODE:
        print(f"[DEBUG] TCP端口通：{ip_port}", flush=True)
    try:
        url = f"http://{ip_port}{url_end}"
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
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
                            "geo": "",
                            "speed": "0.00"
                        }
                    else:
                        if DEBUG_MODE:
                            print(f"[DEBUG] 端口通，但不是udpxy页面：{ip_port}", flush=True)
                else:
                    if DEBUG_MODE:
                        print(f"[DEBUG] HTTP返回非200状态码:{resp.status} {ip_port}", flush=True)
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
async def scan_ip_port(ip_info, port, url_end, mode, max_find, semaphore, global_stop_event, total_valid, max_total_find):
    valid_ip_ports = []
    checked = 0
    total_task = count_total_tasks(ip_info, port, mode)
    task_generator = gen_ip_task(ip_info, port, mode)
    stop_flag = False
    bar_length = 20
    start_time = time.time()
    last_print_pct = 0 # 记录上次打印的5%档位
    async def task_wrapper(ip_port):
        nonlocal checked, stop_flag, last_print_pct
        if stop_flag or global_stop_event.is_set():
            return
        res = await check_udpxy(ip_port, url_end)
        checked += 1
        pct = (checked / total_task)*100
        # ====== 每增加5%打印一次进度 ======
        if int(pct) //5 > last_print_pct:
            last_print_pct = int(pct) //5
            fraction = checked / total_task
            filled_len = int(bar_length * checked / total_task)
            # 色块颜色
            if fraction < 0.33:
                fill_char = "\033[31m█\033[0m"  #红色填充块
            elif fraction < 0.66:
                fill_char = "\033[38;5;94m█\033[0m" #咖啡色
            else:
                fill_char = "\033[32m█\033[0m" #绿色填充块
            bar_fill = fill_char * filled_len
            bar_empty = "░" * (bar_length - filled_len)
            bar_str = bar_fill + bar_empty

            # 百分比文字【加粗亮白】，独立显示，不再嵌入进度条
            percent_text = f"\033[1;97m{pct:.2f}%\033[0m"

            cost = time.time() - start_time
            speed = round(checked / cost, 2)
            total_valid_text = f"| 全局累计:{total_valid[0]}/{max_total_find}" if max_total_find else f"| 全局累计:{total_valid[0]}"
            print(f"📊 进度：{checked}/{total_task} | {percent_text} | 本段存活：{len(valid_ip_ports)} | [{bar_str}] | 速度：{speed}个/秒 {total_valid_text}", flush=True)
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
    pending_tasks = set()
    # 普通for循环遍历生成器
    for ip_port in task_generator:
        if stop_flag or global_stop_event.is_set():
            break
        await semaphore.acquire()
        task = asyncio.create_task(task_wrapper(ip_port))
        pending_tasks.add(task)
        def done_cb(t):
            semaphore.release()
            pending_tasks.remove(t)
        task.add_done_callback(done_cb)
        # 限制pending任务数量
        if len(pending_tasks) >= SEMAPHORE_NUM * 2:
            await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)
    # 如果触发全局停止，直接取消所有剩余任务
    if global_stop_event.is_set() and pending_tasks:
        for t in pending_tasks:
            t.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
    else:
        # 正常等待剩下全部任务完成
        await asyncio.gather(*pending_tasks, return_exceptions=True)
    total_cost = time.time() - start_time
    avg_speed = round(checked / total_cost,2)
    print(f"\n本段扫描结束，总共检测 {checked} 个，有效数量：{len(valid_ip_ports)}，平均速度 {avg_speed}个/秒", flush=True)
    return valid_ip_ports
async def multicast_province(config_file, semaphore, global_stop_event, total_valid, max_total_find):
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
        result = await scan_ip_port(ip_info, port, url_end, mode, max_find, semaphore, global_stop_event, total_valid, max_total_find)
        all_result.extend(result)
    # ====================== 扫描全部完成后，批量查询归属地 ======================
    if GEO_ENABLE and len(all_result) > 0:
        print(f"\n🔍 开始批量查询{len(all_result)}个IP的归属地...", flush=True)
        geo_sem = asyncio.Semaphore(GEO_SEMAPHORE)
        async with aiohttp.ClientSession() as session:
            tasks = []
            for item in all_result:
                async def fill_geo(it):
                    try:
                        it["geo"] = await get_ip_geo(session, it["ip"], geo_sem)
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
    # ========== 双文件输出 ==========
    # 1.纯IP端口txt【测速程序读取，无任何额外内容，保证测速正常】
    with open(f"ip/good_{province}_{operator}_ip.txt", 'w', encoding='utf-8') as f:
        for item in all_result:
            f.write(f"{item['ip_port']}\n")
    # 2.完整信息txt【带归属地，归档查看】
    with open(f"ip/good_{province}_{operator}_full.txt", 'w', encoding='utf-8') as f:
        for item in all_result:
            f.write(f"{item['ip_port']} | 版本:{item['version']} | 最大客户端:{item['max_client']} | 当前连接:{item['current_conn']} | {item['geo']} | 速度:{item['speed']}\n")
    # 3.csv
    csv_path = f"ip/good_{province}_{operator}_result.csv"
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=["ip_port","ip","port","version","max_client","current_conn","geo","speed"])
        writer.writeheader()
        writer.writerows(all_result)
    #4.json
    json_path = f"ip/good_{province}_{operator}_result.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_result, f, ensure_ascii=False, indent=2)
    #5.组播模板
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

# 新增：读取测速结果，合并speed到数据
def load_speed_result(all_items):
    speed_map = {}
    speed_file = "ip/speed_result.txt"
    if not os.path.exists(speed_file):
        print(f"\n⚠️ 测速文件 {speed_file} 不存在，速度保持0.00", flush=True)
        return all_items
    print(f"\n📥 读取测速结果 {speed_file}，合并速度数据", flush=True)
    with open(speed_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 匹配 ip:port | speed:xxx
            match = re.match(r"(.+?)\s*\|\s*speed:([\d\.]+)", line)
            if match:
                ip_port = match.group(1).strip()
                speed_val = match.group(2).strip()
                speed_map[ip_port] = speed_val
    # 回填speed
    for item in all_items:
        if item["ip_port"] in speed_map:
            item["speed"] = speed_map[item["ip_port"]]
    return all_items

async def main():
    semaphore = asyncio.Semaphore(SEMAPHORE_NUM)
    global_stop_event = asyncio.Event()
    total_valid = [0]  # 全局累计有效数，用list包以便协程内修改
    config_files = glob.glob(os.path.join('ip', '*_config.txt'))
    all_provinces_data = []
    for cfg in config_files:
        if global_stop_event.is_set():
            print("全局停止，跳过剩余省份配置文件", flush=True)
            break
        res_data = await multicast_province(cfg, semaphore, global_stop_event, total_valid, MAX_TOTAL_FIND)
        all_provinces_data.extend(res_data)

    # ========= 全部扫描结束后，读取测速结果，重写full.txt =========
    print("\n==== 全部网段扫描完毕，开始合并测速结果 ====")
    all_provinces_data = load_speed_result(all_provinces_data)
    # 重新遍历所有省份，重写full.txt
    config_files = glob.glob(os.path.join('ip', '*_config.txt'))
    for cfg in config_files:
        filename = os.path.basename(cfg)
        province, operator = filename.split('_')[:2]
        # 筛选当前省份的数据
        province_data = [x for x in all_provinces_data if x["geo"]]
        # 重写full.txt，带上速度
        with open(f"ip/good_{province}_{operator}_full.txt", 'w', encoding='utf-8') as f:
            for item in province_data:
                f.write(f"{item['ip_port']} | 版本:{item['version']} | 最大客户端:{item['max_client']} | 当前连接:{item['current_conn']} | {item['geo']} | 速度:{item['speed']}\n")
    print(f"✅ 全部扫描+测速合并完成，总共累计收集有效源 {total_valid[0]} 个", flush=True)
if __name__ == "__main__":
    asyncio.run(main())
