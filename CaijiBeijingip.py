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
PRINT_STEP_PERCENT = 2   # 每5%打印一次进度条
BAR_LENGTH = 40          # 进度条总长度

# ANSI颜色（GitHub Actions支持）
COLOR_GREEN = "\033[42m"
COLOR_YELLOW = "\033[43m"
COLOR_RED = "\033[41m"
COLOR_WHITE_BOLD = "\033[1;37m"
COLOR_RESET = "\033[0m"

# 全局变量
total_tasks = 0
done_tasks = 0
valid_count = 0
last_print_pct = -1

# 归属地session
geo_session = None
geo_sem = None


def parse_octet(s):
    """解析一段，支持 数字 或者 start-end，返回起止"""
    s = s.strip()
    if "-" in s:
        a, b = s.split("-", 1)
        return int(a), int(b)
    else:
        n = int(s)
        return n, n


def expand_ip_range(line):
    """
    支持格式：
    175-178.0-15:4022,0  等价 175-178.0-15.0-255:4022,0
    1.2-5.3.4:4022,1
    返回列表 [(ip, port, flag)]
    """
    line = line.strip()
    pat = re.compile(r"^([\d\-]+(?:\.[\d\-]+)*):(\d+),(\d+)$")
    m = pat.match(line)
    if not m:
        return []
    ip_part = m.group(1)
    port = int(m.group(2))
    flag = m.group(3)

    parts = ip_part.split(".")
    # 不足4段，后面补 0-255
    while len(parts) < 4:
        parts.append("0-255")
    if len(parts) > 4:
        return []

    r0 = parse_octet(parts[0])
    r1 = parse_octet(parts[1])
    r2 = parse_octet(parts[2])
    r3 = parse_octet(parts[3])

    res = []
    for a in range(r0[0], r0[1]+1):
        for b in range(r1[0], r1[1]+1):
            for c in range(r2[0], r2[1]+1):
                for d in range(r3[0], r3[1]+1):
                    ip = f"{a}.{b}.{c}.{d}"
                    res.append((ip, port, flag))
    return res


def make_progress_bar(pct):
    """
    色块铺满进度条框，百分比文字居中，无退格，无乱码
    [████████░░░░░░░░  50.00%  ]
    """
    fill_len = int(BAR_LENGTH * pct / 100)
    filled = "█" * fill_len
    empty = "░" * (BAR_LENGTH - fill_len)
    bar = filled + empty
    text = f"{pct:.2f}%"
    # 居中
    pos = (BAR_LENGTH - len(text)) // 2
    bar_list = list(bar)
    for i, c in enumerate(text):
        if 0 <= pos + i < len(bar_list):
            bar_list[pos + i] = c
    bar_out = "".join(bar_list)
    if pct < 30:
        bg = COLOR_RED
    elif pct < 70:
        bg = COLOR_YELLOW
    else:
        bg = COLOR_GREEN
    return f"[{bg}{COLOR_WHITE_BOLD}{bar_out}{COLOR_RESET}] | 存活:{valid_count}"


async def print_progress():
    global done_tasks, total_tasks, last_print_pct
    if total_tasks == 0:
        return
    pct = done_tasks / total_tasks * 100
    if int(pct // PRINT_STEP_PERCENT) > int(last_print_pct // PRINT_STEP_PERCENT):
        print(make_progress_bar(pct), flush=True)
        last_print_pct = pct


def parse_udpxy_status(html):
    """解析udpxy页面版本信息"""
    res = {}
    footer_re = re.search(r'<div id="footer">udpxy\s+v\.?\s*([\d\.]+)\s*\(Build\s*(\d+)\)\s*(.*?)-\s*\[(.*?)\]', html)
    if footer_re:
        res["version"] = footer_re.group(1)
        res["build"] = footer_re.group(2)
        res["compile_time"] = footer_re.group(4)
    client_re = re.search(r'最大客户端[:：]\s*(\d+)', html)
    if client_re:
        res["max_client"] = client_re.group(1)
    conn_re = re.search(r'当前连接[:：]\s*(\d+)', html)
    if conn_re:
        res["cur_conn"] = conn_re.group(1)
    return res


async def check_udpxy(ip, port):
    global valid_count, done_tasks
    try:
        url = f"http://{ip}:{port}/status"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=HTTP_TIMEOUT) as r:
                if r.status != 200:
                    return None
                text = await r.text()
                info = parse_udpxy_status(text)
                if info:
                    valid_count +=1
                    return {"ip":ip, "port":port, **info}
    except Exception:
        return None
    finally:
        done_tasks +=1
        await print_progress()


async def get_geo(ip):
    if not GEO_ENABLE:
        return ""
    async with geo_sem:
        try:
            url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
            async with geo_session.get(url, timeout=2) as r:
                j = await r.json()
                if j["status"] == "success":
                    return f'{j.get("regionName","")},{j.get("city","")},{j.get("isp","")}'
        except:
            return "未知"
        return "未知"


async def main():
    global total_tasks, geo_session, geo_sem
    input_file = "input.txt"
    out_csv = "result.csv"
    out_txt = "live.txt"

    targets = []
    if os.path.exists(input_file):
        with open(input_file,"r",encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 优先解析 a-b.c-d:e,flag 范围格式
                expanded = expand_ip_range(line)
                if expanded:
                    targets.extend(expanded)
                    continue
                # 兼容旧格式：ip:port
                if ":" in line:
                    ip, p = line.split(":",1)
                    try:
                        port = int(p)
                        targets.append((ip.strip(), port, ""))
                    except:
                        continue
    total_tasks = len(targets)
    print(f"总共扫描任务：{total_tasks}", flush=True)

    geo_sem = asyncio.Semaphore(GEO_SEMAPHORE)
    geo_session = aiohttp.ClientSession()

    sem = asyncio.Semaphore(SEMAPHORE_NUM)
    out_f = open(out_txt,"w",encoding="utf-8")
    csv_f = open(out_csv,"w",encoding="utf-8",newline="")
    csv_w = csv.DictWriter(csv_f, fieldnames=["ip","port","version","build","compile_time","max_client","cur_conn","geo","flag"])
    csv_w.writeheader()

    async def bound_task(ip,port,flag):
        async with sem:
            ret = await check_udpxy(ip,port)
            if ret:
                geo = await get_geo(ip)
                ret["geo"] = geo
                ret["flag"] = flag
                line = f'{ip}:{port},{ret.get("version","")},{geo},{flag}'
                out_f.write(line+"\n")
                out_f.flush()
                csv_w.writerow(ret)
                csv_f.flush()

    task_list = [asyncio.create_task(bound_task(ip,p,f)) for ip,p,f in targets]
    await asyncio.gather(*task_list)
    await geo_session.close()
    out_f.close()
    csv_f.close()
    print("\n✅扫描完成！存活结果保存至 live.txt / result.csv", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
