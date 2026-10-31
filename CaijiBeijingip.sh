#!/bin/bash

# ====================== 全局可配置参数（GA专用）======================
NC_CONCURRENT=12        # 端口探测并发，GA建议 8~12
SPEED_CONCURRENT=10     # 测速curl并发，GA建议 8~10
PROGRESS_STEP=20        # 每处理多少个IP打印一次进度
NC_RETRY=1              # 端口探测失败重试次数（0=关闭重试）
SPEED_RETRY=0           # 测速curl失败重试次数（0=关闭，1=重试1次）
CITY_TOP_COUNT=30       # 单个城市结果保留前N条
MERGE_TOP_COUNT=100     # 合并全部城市后保留前N条
SAVE_LOG=1              # 1=保存日志，0=关闭日志保存
# ====================================================================

# 日志文件名称，带时间戳
LOG_DIR="logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/scan_$(date +%Y%m%d_%H%M%S).log"

# 临时文件列表，用于信号捕获清理
TMP_FILE_LIST=()

# 自定义打印函数【提前放到前面】
log_print(){
    if [ "$SAVE_LOG" -eq 1 ];then
        echo "$1" | tee -a "${LOG_FILE}"
    else
        echo "$1"
    fi
}

# 去除http前缀
strip_http(){
    local str="$1"
    str="${str#http://}"
    str="${str#https://}"
    echo "$str"
}

# ========= 修复cleanup函数：区分异常终止/正常结束 =========
cleanup() {
    local exit_type="$1"
    if [ "$exit_type" -eq 1 ];then
        echo -e "\n⚠️  检测到脚本异常退出，开始清理临时文件..." | tee -a "${LOG_FILE}"
        rm -f tmp_good_ip zubo_*.tmp tmp_all.txt tmp_order_ip.txt tmp_global_sort.txt "${TMP_FILE_LIST[@]}"
        echo "✅ 临时文件清理完成" | tee -a "${LOG_FILE}"
        exit 1
    else
        rm -f tmp_good_ip zubo_*.tmp tmp_all.txt tmp_order_ip.txt tmp_global_sort.txt
        log_print "✅ 当前城市临时文件清理完毕，准备进入下一个城市"
    fi
}

# 捕获异常信号
trap 'cleanup 1' SIGINT SIGTERM SIGHUP

# ========= 每次执行前备份旧zubo.txt =========
if [ -f "zubo.txt" ];then
    log_print "📦 检测到上一轮结果，备份旧文件 -> zubo_bf.txt"
    cp -f zubo.txt zubo_bf.txt
fi

if [ $# -eq 0 ]; then
  log_print "开始测试······"
  log_print "在5秒内输入城市编号，0=批量跑全部城市"
  log_print "1:北京  2:上海  3:广州"
  read -t 5 -p "超时未输入,默认批量跑全部城市(0)" city_choice

  if [ -z "$city_choice" ]; then
      log_print "未检测到输入，批量执行全部城市"
      city_choice=0
  fi
else
  city_choice=$1
fi

mkdir -p ip txt template

# ========= 多城市配置 =========
case $city_choice in
    1)
        city="Beijing_dianxin"
        stream="rtp/239.94.0.103:5140"
        ;;
    2)
        city="Shanghai_dianxin"
        stream="rtp/替换成上海组播地址"
        ;;
    3)
        city="Guangzhou_dianxin"
        stream="rtp/替换成广州组播地址"
        ;;
    0)
        for option in 1 2 3; do
          bash "$0" "$option"
        done
        log_print "==================== 所有城市测速全部完成，开始全局合并排序 ===================="
        ;;
    *)
        log_print "⚠️ 城市编号【$city_choice】未配置，直接退出"
        cleanup 1
        ;;
esac

# ========== 全局合并 zubo.txt 【全局保持测速降序】 ==========
if [ "$city_choice" -eq 0 ];then
    merge_output="txt/all_city_sorted.txt"
    > $merge_output

    cat ip/result_*.txt | sort -k1 -nr | awk '!seen[$3]++' | head -n ${MERGE_TOP_COUNT} > tmp_global_sort.txt

    > $merge_output
    while read byte speed ip_raw;do
        ip_raw=$(strip_http "$ip_raw")
        ip_only=$(echo "$ip_raw" | cut -d: -f1)
        first_oct=$(echo "$ip_only" | cut -d. -f1)
        if [ "$first_oct" -ge 224 ] && [ "$first_oct" -le 239 ];then
            continue
        fi
        echo "${ip_raw}" >> $merge_output
    done < tmp_global_sort.txt
    rm -f tmp_global_sort.txt

    cp $merge_output zubo.txt

    log_print "✅ 全局合并完成，IP按测速由快到慢排序，去重，过滤组播，保留前${MERGE_TOP_COUNT}条"
    log_print "📝 日志保存至：${LOG_FILE}"
    exit 0
fi

# =========【单城市测速逻辑】=========
if [ -z "$city" ];then
    log_print "❌ city变量为空，退出本次扫描"
    cleanup 1
fi

time=$(date +%m%d%H%M)
good_ip=ip/good_${city}_ip.txt
result_ip=ip/result_${city}_ip.txt
speed_log=speedtest_${city}_$time.log
> $speed_log

log_print "======== 开始检索 ${city} ========"
if [ ! -f "$good_ip" ];then
    log_print "❌ 错误：存活IP文件 $good_ip 不存在！跳过该城市"
    cleanup 0
fi

sort "$good_ip" | uniq | sed '/^\s*$/d' > tmp_filter_ip
mv tmp_filter_ip "$good_ip"
lines=$(wc -l < "$good_ip")
log_print "存活IP总数：$lines"

if [ "$lines" -eq 0 ];then
    log_print "⚠️ 存活IP为空，跳过城市"
    cleanup 0
fi

speed_test(){
    local ip="$1"
    local idx="$2"
    local total="$3"
    local url="http://$ip/$stream"
    local raw_byte=""
    raw_byte=$(curl --connect-timeout 3 --max-time 15 -o /dev/null -w "%{speed_download}" "$url" 2>/dev/null)
    if [[ "$raw_byte" =~ ^[0-9\.]+$ && $(echo "$raw_byte > 0" | bc) -eq 1 ]];then
        # 格式化输出用于日志展示
        if (( $(echo "$raw_byte >= 1048576" | bc -l) )); then
            display_speed=$(echo "scale=2; $raw_byte / 1048576" | bc)M
        elif (( $(echo "$raw_byte >= 1024" | bc -l) )); then
            display_speed=$(echo "scale=2; $raw_byte / 1024" | bc)k
        else
            display_speed="${raw_byte}B"
        fi
        # 存入日志：【原始字节数】【格式化速度】【IP】
        echo "$raw_byte $display_speed $ip" >> $speed_log
        log_print "第${idx}/${total}个：$ip | $display_speed"
    fi
}

i=0
total_good=$lines
while read line; do
    i=$((i + 1))
    ip=$line
    speed_test "$ip" "$i" "$total_good" &
    if [ $((i % PROGRESS_STEP)) -eq 0 ];then wait; fi
    if [ $((i % SPEED_CONCURRENT)) -eq 0 ];then wait; fi
done < $good_ip
wait
log_print "[测速进度] $i/$total_good (100%) 测速完成"

# 重点！按【原始字节数值】降序排序，存入result_ip
sort -k1 -nr $speed_log > $result_ip

# ====================== 【新增：打印 测速结果排序 区块到日志】======================
log_print ""
log_print "测速结果排序"
# 过滤组播IP，输出：速度  IP:端口，和截图格式一模一样
awk '
{
    ip=$3
    split(ip,a,":");
    split(a[1],oct,".");
    if(!(oct[1]>=224 && oct[1]<=239)) print $2,"  ",$3
}' $result_ip | head -n ${CITY_TOP_COUNT} | while read line;do
    log_print "$line"
done
log_print ""
# =================================================================================

# ========= 保留排序顺序生成城市txt文件（纯净IP:PORT，不带http，无多余文字） =========
awk '
{
    ip=$3
    split(ip,a,":");
    split(a[1],oct,".");
    if(!(oct[1]>=224 && oct[1]<=239)) print ip
}' $result_ip | head -n ${CITY_TOP_COUNT} > tmp_order_ip.txt

cp tmp_order_ip.txt "txt/${city}.txt"
rm -f tmp_order_ip.txt

rm -f $speed_log
log_print "${city} 完成，输出txt文件保持测速从快到慢顺序"

cleanup 0
