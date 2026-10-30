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

# ========= 修复cleanup函数：区分异常终止/正常结束 =========
# $1=1 异常退出；$1=0 正常跑完当前城市
cleanup() {
    local exit_type="$1"
    if [ "$exit_type" -eq 1 ];then
        echo -e "\n⚠️  检测到脚本异常退出，开始清理临时文件..." | tee -a "${LOG_FILE}"
        rm -f tmp_good_ip zubo_*.tmp tmp_all.txt "${TMP_FILE_LIST[@]}"
        echo "✅ 临时文件清理完成" | tee -a "${LOG_FILE}"
        exit 1
    else
        # 正常跑完单个城市：清理当前城市临时文件，不exit，返回主循环继续跑下一个城市
        rm -f tmp_good_ip zubo_*.tmp tmp_all.txt
        log_print "✅ 当前城市临时文件清理完毕，准备进入下一个城市"
    fi
}

# 捕获异常信号: SIGINT(Ctrl+C), SIGTERM(进程被杀), SIGHUP
trap 'cleanup 1' SIGINT SIGTERM SIGHUP

# 自定义打印函数，同时输出控制台+写入日志
log_print(){
    if [ "$SAVE_LOG" -eq 1 ];then
        echo "$1" | tee -a "${LOG_FILE}"
    else
        echo "$1"
    fi
}

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

# 自动创建目录防止报错
mkdir -p ip txt template

# ========= 多城市配置，自行修改stream组播地址 =========
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
        # 批量入口：串行依次跑1北京,2上海,3广州
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

# 如果是批量入口(0)，执行完循环就进入全局合并逻辑
if [ "$city_choice" -eq 0 ];then
    #--------合并所有城市txt文件 + 按带宽降序，IP:端口相同才去重，同IP不同端口保留，保留前MERGE_TOP_COUNT条--------
    merge_output="txt/all_city_sorted.txt"
    > $merge_output

    cat txt/*.txt | awk -v top="$MERGE_TOP_COUNT" '
    function getnum(s){
        if(s ~ /M/){return s*1024}
        if(s ~ /k/){return s}
        return 0
    }
    {
        rate=""
        for(i=1;i<=NF;i++){
            if($i ~ /^[0-9]+\.?[0-9]*[kM]$/){
                rate=$i
                break
            }
        }
        # 提取ip:port作为唯一key
        ip_port=""
        for(i=1;i<=NF;i++){
            if($i ~ /[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+/){
                ip_port=$i
                break
            }
        }
        num=getnum(rate)
        print num, ip_port, $0
    }' | sort -k1 -nr | awk -v top="$MERGE_TOP_COUNT" '
    BEGIN{count=0}
    {
        key=$2
        if( !seen[key]++ ){
            if(count < top){
                print substr($0, index($0,$3))
                count++
            }
        }
    }' > $merge_output

    log_print "✅ 全局合并完成，IP+端口完全相同才去重，同IP不同端口保留，只保留最快前${MERGE_TOP_COUNT}条：$merge_output"
    # ============新增：打印全局合并后的总排序列表到日志============
    log_print "==================== 全局合并【从快到慢总清单】 ===================="
    cat "${merge_output}" | tee -a "${LOG_FILE}"
    log_print "====================================================================="

    log_print "📝 本次扫描日志保存至：${LOG_FILE}"
    exit 0
fi

# =========【单城市测速逻辑开始】=========
if [ -z "$city" ];then
    log_print "❌ city变量为空，退出本次扫描"
    cleanup 1
fi

# 使用城市名作为默认文件名，格式为 CityName.ip
time=$(date +%m%d%H%M)
good_ip=ip/good_${city}_ip.txt
result_ip=ip/result_${city}_ip.txt
speed_log=speedtest_${city}_$time.log
> $speed_log

log_print "======== 开始检索 ${city} ========"
log_print "从 '${good_ip}' 读取Python预扫描完成的存活IP，跳过端口探测，直接测速"

# =========【文件防护】=========
if [ ! -f "$good_ip" ];then
    log_print "❌ 错误：存活IP文件 $good_ip 不存在！跳过该城市"
    cleanup 0
fi

# 去重、清除空行
sort "$good_ip" | uniq | sed '/^\s*$/d' > tmp_filter_ip
mv tmp_filter_ip "$good_ip"

lines=$(wc -l < "$good_ip")
log_print "获取存活IP总数：$lines"

# ========== 调试：打印全部待测速IP列表 ==========
log_print "===== 调试：本次待测速IP列表 ====="
cat "$good_ip" | tee -a "${LOG_FILE}"
log_print "=================================="

if [ "$lines" -eq 0 ];then
    log_print "⚠️ 存活IP文件为空，跳过当前城市扫描"
    cleanup 0
fi

log_print "端口探测由Python脚本提前完成，开始并行测速，并发数: $SPEED_CONCURRENT，测速重试次数:$SPEED_RETRY"

# 并行测速函数
speed_test(){
    local ip="$1"
    local idx="$2"
    local total="$3"
    local url="http://$ip/$stream"
    local raw_speed=""
    # 测速重试循环
    for ((r=0; r<=SPEED_RETRY; r++)); do
        raw_speed=$(curl --connect-timeout 3 --max-time 15 -o /dev/null -w "%{speed_download}" "$url" 2>/dev/null)
        if [[ "$raw_speed" =~ ^[0-9\.]+$ && $(echo "$raw_speed > 0" | bc) -eq 1 ]];then
            break
        fi
    done

    # 只过滤0，大于0全部写入日志
    if [[ "$raw_speed" =~ ^[0-9\.]+$ && $(echo "$raw_speed > 0" | bc) -eq 1 ]]; then
        if (( $(echo "$raw_speed >= 1048576" | bc -l) )); then
            a=$(echo "scale=2; $raw_speed / 1048576" | bc)M
        elif (( $(echo "$raw_speed >= 1024" | bc -l) )); then
            a=$(echo "scale=2; $raw_speed / 1024" | bc)k
        else
            a="${raw_speed}B"
        fi
        local line="第$idx/$total个：$ip    $a"
        echo "$line" >> $speed_log
        log_print "$line"
    else
        log_print "第$idx/$total个：$ip    0B 已跳过"
    fi
}

i=0
total_good=$lines
while read line; do
    i=$((i + 1))
    ip=$line
    speed_test "$ip" "$i" "$total_good" &
    if [ $((i % PROGRESS_STEP)) -eq 0 ];then
        wait
        pct=$(( i * 100 / total_good ))
        log_print "[测速进度] $i/$total_good ($pct%)"
    fi
    if [ $((i % SPEED_CONCURRENT)) -eq 0 ];then
        wait
    fi
done < $good_ip
wait
log_print "[测速进度] $i/$total_good (100%) 测速全部完成"

log_print "测速结果排序"
# 转换k/M为数值用于排序：k=千，M=兆，统一转成KB排序
awk '
{
    for(i=1;i<=NF;i++){
        if ($i ~ /[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+/) ip=$i
        if ($i ~ /^[0-9]+\.?[0-9]*[kM]$/) speed=$i
    }
    if(speed == "") next;
    if(speed ~ /M/){num=speed*1024}
    else if(speed ~ /k/){num=speed}
    else {num=0}
    print num,speed,ip
}
' $speed_log | sort -k1 -nr | awk '{print $2"  "$3}' > $result_ip

# 打印当前城市排序后的速度列表
log_print "========= ${city} 排序完成，从快到慢列表 ========="
cat "${result_ip}" | tee -a "${LOG_FILE}"
log_print "=================================================="

# ========== 单个城市取前 CITY_TOP_COUNT 条，生成城市独立txt ==========
> tmp_all.txt
for ((n=1; n<=CITY_TOP_COUNT; n++));do
    ip=$(awk -v line="$n" 'NR==line{print $2}' $result_ip)
    if [ -z "$ip" ];then continue;fi
    sed "s|ipipip|$ip|g" template/template_${city}.txt >> tmp_all.txt
done
# 过滤包含连续三个斜杠的脏行 http:///
grep -v '///' tmp_all.txt > "txt/${city}.txt"
rm -f tmp_all.txt $speed_log $result_ip
log_print "${city} 测试完成，保留前${CITY_TOP_COUNT}条，生成文件：'txt/${city}.txt'"

# 正常结束，调用cleanup，不退出脚本，返回批量循环
cleanup 0
