#!/bin/bash

# ====================== 全局可配置参数（GA专用）======================
NC_CONCURRENT=12        # 端口探测并发，GA建议 8~12
SPEED_CONCURRENT=10     # 测速curl并发，GA建议 8~10
PROGRESS_STEP=20        # 每处理多少个IP打印一次进度
NC_RETRY=1              # nc端口探测失败重试次数（0=关闭重试）
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
# 捕获信号：Ctrl+C、脚本崩溃、程序终止，触发清理函数
cleanup() {
    echo -e "\n⚠️  检测到脚本退出，开始清理临时文件..." | tee -a "${LOG_FILE}"
    rm -f tmp_good_ip zubo_*.tmp tmp_all.txt "${TMP_FILE_LIST[@]}"
    echo "✅ 临时文件清理完成" | tee -a "${LOG_FILE}"
    exit
}
# 注册捕获信号: SIGINT(Ctrl+C), SIGTERM(进程被杀), SIGHUP
trap cleanup SIGINT SIGTERM SIGHUP

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
  log_print "在5秒内输入1~4可选择城市"
  log_print "请选择城市："
  echo "1. 北京（Beijing_dianxin）"
  read -t 5 -p "超时未输入,将按默认设置测试" city_choice

  if [ -z "$city_choice" ]; then
      log_print "未检测到输入,默认测试全部"
      city_choice=0
  fi

else
  city_choice=$1
fi

# 自动创建目录防止报错
mkdir -p ip txt template

# 设置城市和相应的stream
case $city_choice in
    1)
        city="Beijing_dianxin"
        stream="rtp/239.77.0.217:5146"
        ;;
    0)
        # 循环调用所有城市，修复递归
        for option in {1..35}; do
          bash "$0" $option
        done
        exit 0
        ;;
esac

# 使用城市名作为默认文件名，格式为 CityName.ip
time=$(date +%m%d%H%M)
ipfile=ip/${city}_ip.txt
good_ip=ip/good_${city}_ip.txt
result_ip=ip/result_${city}_ip.txt
speed_log=speedtest_${city}_$time.log
> $speed_log
> $good_ip

log_print "======== 开始检索 ${city} ========"

log_print "从 '${ipfile}' 读取ip并添加到检测列表"
cat $ipfile >> tmp_ipfile
sort tmp_ipfile | uniq | sed '/^\s*$/d' > $ipfile
rm -f tmp_ipfile

# 获取IP总数用于进度百分比
total_ip=$(wc -l < "$ipfile")
log_print "待探测IP总数：$total_ip"

# ====================== 【并发端口探测 nc + 重试 + 进度输出】======================
log_print "开始并发端口探测，并发数：$NC_CONCURRENT，失败重试次数：$NC_RETRY"
> tmp_good_ip
task_nc(){
    local ip="$1"
    local tmp_ip=$(echo -n "$ip" | sed 's/:/ /')
    local output=""
    local ok=0
    # 循环重试
    for ((r=0; r<=NC_RETRY; r++)); do
        output=$(nc -w 1 -v -z $tmp_ip 2>&1)
        if [[ $output == *"succeeded"* ]]; then
            ok=1
            break
        fi
    done
    if [ $ok -eq 1 ]; then
        echo "$ip" >> tmp_good_ip
    fi
}

i=0
while IFS= read -r ip; do
    i=$((i + 1))
    task_nc "$ip" &
    # 每PROGRESS_STEP输出进度
    if [ $((i % PROGRESS_STEP)) -eq 0 ];then
        wait
        pct=$(( i * 100 / total_ip ))
        log_print "[端口探测进度] $i/$total_ip ($pct%)"
    fi
    # 控制并发上限
    if [ $((i % NC_CONCURRENT)) -eq 0 ];then
        wait
    fi
done < $ipfile
wait
# 最后一次进度
pct=$(( i * 100 / total_ip ))
log_print "[端口探测进度] $i/$total_ip ($pct%) 探测全部完成"

# 去重写入good_ip
sort tmp_good_ip | uniq > $good_ip
rm -f tmp_good_ip
# ===============================================================

lines=$(wc -l < $good_ip)
log_print "端口探测完成，连接成功 $lines 个,开始并行测速，并发数: $SPEED_CONCURRENT，测速重试次数:$SPEED_RETRY"

# 并行测速函数（增加测速重试逻辑）
speed_test(){
    local ip="$1"
    local idx="$2"
    local total="$3"
    local url="http://$ip/$stream"
    local a=""
    # 测速重试循环
    for ((r=0; r<=SPEED_RETRY; r++)); do
        curl $url --connect-timeout 3 --max-time 15 -o /dev/null >zubo_${idx}.tmp 2>&1
        a=$(head -n 3 zubo_${idx}.tmp | awk '{print $NF}' | tail -n 1)
        # 如果获取到速率，直接跳出重试
        if [[ $a =~ [kM] ]];then
            break
        fi
    done
    echo "第$idx/$total个：$ip    $a" >> $speed_log
}

i=0
total_good=$lines
while read line; do
    i=$((i + 1))
    ip=$line
    # 后台跑测速任务，控制并发
    speed_test "$ip" "$i" "$total_good" &
    # 测速阶段进度打印
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
    # 从行内提取IP和速率
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

# ========== 单个城市取前 CITY_TOP_COUNT 条，生成城市独立txt ==========
> tmp_all.txt
for ((n=1; n<=CITY_TOP_COUNT; n++));do
    ip=$(awk -v line="$n" 'NR==line{print $2}' $result_ip)
    if [ -z "$ip" ];then continue;fi
    # 改用 | 做sed分隔符，避免IP和/冲突
    sed "s|ipipip|$ip|g" template/template_${city}.txt >> tmp_all.txt
done
# 过滤包含连续三个斜杠的脏行 http:///
grep -v '///' tmp_all.txt > "txt/${city}.txt"
rm -f tmp_all.txt $ipfile $good_ip $speed_log $result_ip
log_print "${city} 测试完成，保留前${CITY_TOP_COUNT}条，生成文件：'txt/${city}.txt'"


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

log_print "✅ 全部城市合并完成，IP+端口完全一致才去重，同IP不同端口保留，只保留最快前${MERGE_TOP_COUNT}条：$merge_output"
log_print "📝 本次扫描日志保存至：${LOG_FILE}"

# 正常执行完毕也执行一次清理
cleanup
