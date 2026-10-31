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

# 去除http前缀的工具函数，返回纯ip:port
strip_http(){
    local str="$1"
    str="${str#http://}"
    str="${str#https://}"
    echo "$str"
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
        stream="rtp/233.18.204.67:5140"
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

# ===================== 全局合并部分（生成zubo.txt，格式完全和Beijing_dianxin.txt一致）=====================
if [ "$city_choice" -eq 0 ];then
    merge_raw="tmp_merge_raw.txt"
    > "$merge_raw"

    log_print "====调试：查看ip目录速度文件===="
    ls -la ip/
    log_print "====调试：速度文件原始内容===="
    cat ip/tmp_speed_*.txt | tee -a "${LOG_FILE}"
    log_print "=============================="

    # 读取每个城市保存好的【速度 + ip:port】进行全局排序
    cat ip/tmp_speed_*.txt | awk '
    function getnum(s){
        if(s ~ /M/){return s*1024}
        if(s ~ /k/){return s}
        return 0
    }
    {
        speed_val=$1
        ip_port=$2
        num=getnum(speed_val)
        print num, ip_port
    }' | sort -k1 -nr | awk -v top="$MERGE_TOP_COUNT" '
    BEGIN{count=0}
    !seen[$2]++{
        if(count < top){
            print $2
            count++
        }
    }' > tmp_sort

    # 过滤组播IP段224~239，得到纯净裸ip:port列表
    awk '
    {
        split($1,iparr,":");
        split(iparr[1],oct,".");
        if(oct[1]>=224 && oct[1]<=239) next;
        print $1
    }' tmp_sort > "$merge_raw"
    rm -f tmp_sort

    # ===== 使用北京模板生成zubo.txt，格式和Beijing_dianxin.txt一模一样 =====
    > zubo.txt
    while read -r ip_raw; do
        ip_full="http://${ip_raw}"
        sed "s|ipipip|$ip_full|g" template/template_Beijing_dianxin.txt >> zubo.txt
    done < "$merge_raw"
    rm -f "$merge_raw"

    log_print "✅ 全局合并完成，zubo.txt 使用#EXTINF模板格式，保留前${MERGE_TOP_COUNT}条"
    log_print "==================== 全局合并完成 ===================="

    # 清理临时速度文件和垃圾文件
    rm -f ip/tmp_speed_*.txt
    rm -f zubo_bf.txt
    rm -f 组播_*.txt

    log_print "📝 本次扫描日志保存至：${LOG_FILE}"
    exit 0
fi

# =========【单城市测速逻辑开始】=========
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
log_print "从 '${good_ip}' 读取Python预扫描存活IP，直接测速"

if [ ! -f "$good_ip" ];then
    log_print "❌ 错误：存活IP文件 $good_ip 不存在，跳过该城市"
    cleanup 0
fi

sort "$good_ip" | uniq | sed '/^\s*$/d' > tmp_filter_ip
mv tmp_filter_ip "$good_ip"

lines=$(wc -l < "$good_ip")
log_print "获取待测速IP总数：$lines"

log_print "===== 待测速IP列表 ====="
cat "$good_ip" | tee -a "${LOG_FILE}"
log_print "========================"

if [ "$lines" -eq 0 ];then
    log_print "⚠️ IP列表为空，跳过当前城市"
    cleanup 0
fi

log_print "开始并发测速，并发数：${SPEED_CONCURRENT}"

speed_test(){
    local ip="$1"
    local url="http://$ip/$stream"
    local raw_speed=""
    # 打印当前正在测速的IP（输出到stderr，不污染测速结果文件）
    echo "🔍 正在测速: $ip" >&2
    for ((r=0; r<=SPEED_RETRY; r++)); do
        raw_speed=$(curl --connect-timeout 3 --max-time 10 -o /dev/null -w "%{speed_download}" "$url" 2>/dev/null)
        if [[ "$raw_speed" =~ ^[0-9\.]+$ && $(echo "$raw_speed > 0" | bc) -eq 1 ]];then
            break
        fi
    done

    if [[ "$raw_speed" =~ ^[0-9\.]+$ && $(echo "$raw_speed > 0" | bc) -eq 1 ]]; then
        if (( $(echo "$raw_speed >= 1048576" | bc -l) )); then
            a=$(echo "scale=2; $raw_speed / 1048576" | bc)M
        elif (( $(echo "$raw_speed >= 1024" | bc -l) )); then
            a=$(echo "scale=2; $raw_speed / 1024" | bc)k
        else
            a="${raw_speed}B"
        fi
        # 输出到测速结果文件
        echo "$a $ip"
        # 成功日志，stderr输出，仅控制台展示
        echo "✅ 测速成功 $ip 速度: $a" >&2
    else
        # 测速失败日志，stderr输出，仅控制台展示
        echo "❌ 测速失败 $ip raw_speed:$raw_speed" >&2
    fi
}
export -f speed_test
export SPEED_RETRY stream

# 并发测速
cat "$good_ip" | xargs -P ${SPEED_CONCURRENT} -I {} bash -c 'speed_test "{}"' > $speed_log

log_print "测速完成，开始按速度降序排序"
# 按速度从快到慢排序
sort -k1 -nr $speed_log > $result_ip

# 保存tmp_speed给全局合并使用
cp "$result_ip" "ip/tmp_speed_${city}.txt"

log_print "========= ${city} 测速排序结果 ========="
cat "${result_ip}" | tee -a "${LOG_FILE}"
log_print "========================================"

# 生成城市txt文件，使用模板
> tmp_all.txt
for ((n=1; n<=CITY_TOP_COUNT; n++));do
    ip_raw=$(awk -v line="$n" 'NR==line{print $2}' "$result_ip")
    ip_raw=$(strip_http "$ip_raw")
    ip_only=$(echo "$ip_raw" | cut -d: -f1)
    first_oct=$(echo "$ip_only" | cut -d. -f1)
    # 过滤组播IP
    if [ "$first_oct" -ge 224 ] && [ "$first_oct" -le 239 ];then
        continue
    fi
    if [ -z "$ip_raw" ];then continue;fi
    ip_full="http://${ip_raw}"
    sed "s|ipipip|$ip_full|g" template/template_${city}.txt >> tmp_all.txt
done

# 输出txt文件
sed 's|第[0-9]*\/[0-9]*个：||g' tmp_all.txt | grep -v '///' > "txt/${city}.txt"
rm -f tmp_all.txt $speed_log $result_ip
log_print "${city} 测速完成，文件输出至 txt/${city}.txt"

cleanup 0
