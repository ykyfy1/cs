#!/bin/bash

# ====================== 全局可配置参数（GA专用）======================
NC_CONCURRENT=12        # 端口探测并发，GA建议 8~12
SPEED_CONCURRENT=10     # 测速curl并发，GA建议 8~10
PROGRESS_STEP=20        # 每处理多少个IP打印一次进度
NC_RETRY=1              # 端口探测失败重试次数（0=关闭重试）
SPEED_RETRY=0           # 测速curl失败重试次数（0=关闭，1=重试1次）
CITY_TOP_COUNT=30       # 单个城市测速时最多采集条数
MERGE_TOP_COUNT=100     # 全局合并最终保留前N条
SAVE_LOG=1              # 1=保存日志，0=关闭日志保存
# ====================================================================

# 日志文件名称，带时间戳
LOG_DIR="logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/scan_$(date +%Y%m%d_%H%M%S).log"

# ========= 清理函数 =========
cleanup() {
    local exit_type="$1"
    if [ "$exit_type" -eq 1 ];then
        echo -e "\n⚠️  检测到异常退出，清理临时文件..." | tee -a "${LOG_FILE}"
        rm -f tmp_good_ip zubo_*.tmp tmp_all.txt tmp_speed_m3u.tmp
        echo "✅ 临时文件清理完成" | tee -a "${LOG_FILE}"
        exit 1
    else
        rm -f tmp_good_ip zubo_*.tmp tmp_all.txt
        log_print "✅ 当前城市临时文件清理完成，继续下一城市"
    fi
}

# 捕获异常信号
trap 'cleanup 1' SIGINT SIGTERM SIGHUP

# 日志打印函数
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

if [ $# -eq 0 ]; then
  log_print "开始测试······"
  log_print "5秒内输入城市编号，0=批量跑全部城市"
  log_print "1:北京  2:上海  3:广州"
  read -t 5 -p "输入编号：" city_choice
  if [ -z "$city_choice" ]; then
      log_print "无输入，默认批量执行全部城市"
      city_choice=0
  fi
else
  city_choice="$1"
fi

# 创建目录
mkdir -p ip txt template

# ========= 城市配置 =========
case $city_choice in
    1)
        city="Beijing_dianxin"
        stream="rtp/239.94.0.103:5140"
        ;;
    2)
        city="Shanghai_dianxin"
        stream="rtp/替换上海组播地址"
        ;;
    3)
        city="Guangzhou_dianxin"
        stream="rtp/替换广州组播地址"
        ;;
    0)
        log_print "==================== 开始依次测速全部城市 ===================="
        # 串行跑完北京、上海、广州测速
        for option in 1 2 3; do
          bash "$0" "$option"
        done
        log_print "==================== 全部城市测速结束，生成全局M3U排序列表 ===================="
        merge_output="txt/all_city_sorted.m3u"
        > "$merge_output"
        tmp_speed_m3u="tmp_speed_m3u.tmp"
        > "$tmp_speed_m3u"

        # 读取每个城市生成的临时m3u，提取速度、完整两行m3u内容
        cat txt/*.txt | awk '
        BEGIN{
            speed_val=0
            line_buf=""
        }
        {
            if($0 ~ /^#EXTINF/){
                line_buf=$0
            }else if($0 ~ /^http:\/\//){
                # 拿到http行，提取IP
                ip_line=$0
                sub(/http:\/\//,"",ip_line)
                split(ip_line,ipp,":")
                ip=ipp[1]
                split(ip,oct,".")
                # 过滤组播IP
                if(oct[1]>=224 && oct[1]<=239) {line_buf="";next;}
                print speed_val, line_buf
                print speed_val, $0
                speed_val=0
                line_buf=""
            }else if($0 ~ /[0-9]+\.?[0-9]*[kM]/){
                # 提取速度
                if($0 ~ /M/){sub(/M/,"");speed_val=$0*1024}
                else if($0 ~ /k/){sub(/k/,"");speed_val=$0}
                else{speed_val=0}
            }
        }' > "${tmp_speed_m3u}"

        # 按速度降序排序，重组标准m3u格式
        cat "${tmp_speed_m3u}" | sort -k1 -nr | awk '
        BEGIN{last_speed=-1}
        {
            sp=$1
            content=substr($0,index($0,$2))
            if(sp != last_speed){
                print content
            }else{
                print content
            }
            last_speed=sp
        }' | grep -v '^[0-9.]*$' > "${merge_output}"

        # ===================== 输出m3u文件，保留#EXTINF格式 =====================
        cp "$merge_output" zubo.m3u
        cp "$merge_output" txt/Beijing_dianxin.m3u
        cp "$merge_output" txt/Shanghai_dianxin.m3u
        cp "$merge_output" txt/Guangzhou_dianxin.m3u
        cp "$merge_output" 组播_Beijing_dianxin.m3u
        cp "$merge_output" 组播_Shanghai_dianxin.m3u
        cp "$merge_output" 组播_Guangzhou_dianxin.m3u

        # 输出行数校验，GA日志直接看有没有数据
        log_print "==== 文件生成校验 ===="
        log_print "zubo.m3u 行数：$(wc -l < zubo.m3u)"
        log_print "all_city_sorted.m3u 行数：$(wc -l < ${merge_output})"
        log_print "组播_Beijing_dianxin.m3u 行数：$(wc -l < 组播_Beijing_dianxin.m3u)"
        log_print "======================"

        # 备份上一轮结果
        cp zubo.m3u zubo_bf.m3u

        log_print "✅ 全局排序完成！保留M3U格式，按测速从快到慢排序"
        log_print "👉 zubo.m3u / txt/下城市文件 / 组播_*.m3u 内容完全相同"

        # 打印预览
        log_print "===== 全局M3U排序列表预览 ====="
        cat "$merge_output" | tee -a "${LOG_FILE}"
        log_print "====================================================================="
        log_print "📝 日志已保存: ${LOG_FILE}"
        exit 0
        ;;
    *)
        log_print "⚠️ 城市编号【$city_choice】不存在，退出"
        cleanup 1
        ;;
esac

# ========= 单城市测速逻辑 =========
if [ -z "$city" ];then
    log_print "❌ city变量为空，退出"
    cleanup 1
fi

time_tag=$(date +%m%d%H%M)
good_ip="ip/good_${city}_ip.txt"
result_ip="ip/result_${city}_ip.txt"
speed_log="speedtest_${city}_${time_tag}.log"
city_m3u_tmp="txt/${city}.txt"
> "$speed_log"
> "$city_m3u_tmp"

log_print "======== 开始测速 ${city} ========"
log_print "读取存活IP文件: ${good_ip}"

if [ ! -f "$good_ip" ];then
    log_print "❌ 文件不存在：$good_ip，跳过该城市"
    cleanup 0
fi

sort "$good_ip" | uniq | sed '/^\s*$/d' > tmp_filter_ip && mv tmp_filter_ip "$good_ip"
line_count=$(wc -l < "$good_ip")
log_print "存活IP数量：$line_count"
log_print "==== 待测速IP列表 ===="
cat "$good_ip" | tee -a "${LOG_FILE}"
log_print "======================"

if [ "$line_count" -eq 0 ];then
    log_print "⚠️ IP文件为空，跳过"
    cleanup 0
fi

log_print "开始并发测速，并发数：${SPEED_CONCURRENT}"

speed_test(){
    local ip="$1"
    local idx="$2"
    local total="$3"
    local url="http://${ip}/${stream}"
    local raw_speed=""
    for ((r=0; r<=SPEED_RETRY; r++)); do
        raw_speed=$(curl --connect-timeout 3 --max-time 15 -o /dev/null -w "%{speed_download}" "$url" 2>/dev/null)
        if [[ "$raw_speed" =~ ^[0-9\.]+$ && $(echo "$raw_speed > 0" | bc) -eq 1 ]];then
            break
        fi
    done

    if [[ "$raw_speed" =~ ^[0-9\.]+$ && $(echo "$raw_speed > 0" | bc) -eq 1 ]]; then
        if (( $(echo "$raw_speed >= 1048576" | bc -l) )); then
            sp=$(echo "scale=2; $raw_speed / 1048576" | bc)
            unit="M"
        elif (( $(echo "$raw_speed >= 1024" | bc -l) )); then
            sp=$(echo "scale=2; $raw_speed / 1024" | bc)
            unit="k"
        else
            sp="$raw_speed"
            unit="B"
        fi
        echo "${sp}${unit} ${ip}" >> "$speed_log"
        log_print "第${idx}/${total}个：${ip}    ${sp}${unit}"
    else
        log_print "第${idx}/${total}个：${ip}    0B 已跳过"
    fi
}

i=0
while read -r line; do
    i=$((i+1))
    ip="$line"
    speed_test "$ip" "$i" "$line_count" &
    if (( i % PROGRESS_STEP == 0 ));then
        wait
        pct=$(( i * 100 / line_count ))
        log_print "[测速进度] ${i}/${line_count} (${pct}%)"
    fi
    if (( i % SPEED_CONCURRENT == 0 ));then
        wait
    fi
done < "$good_ip"
wait
log_print "[测速进度] ${i}/${line_count} (100%) 当前城市测速完成"

log_print "开始本城市测速结果排序"
# 按速度降序排序
awk '
{
    ip=$2
    speed=$1
    if(speed ~ /M/){num=speed*1024}
    else if(speed ~ /k/){num=speed}
    else {num=0}
    print num,speed,ip
}
' "$speed_log" | sort -k1 -nr > "$result_ip"

# 读取template模板生成M3U两行一组格式，写入城市txt
for ((n=1; n<=CITY_TOP_COUNT; n++));do
    ip_raw=$(awk -v line="$n" 'NR==line{print $3}' "$result_ip")
    speed_line=$(awk -v line="$n" 'NR==line{print $2}' "$result_ip")
    ip_raw=$(strip_http "$ip_raw")
    if [ -z "$ip_raw" ];then
        continue
    fi
    ip_only=$(echo "$ip_raw" | cut -d: -f1)
    first_oct=$(echo "$ip_only" | cut -d. -f1)
    if [ -n "$first_oct" ] && [ "$first_oct" -ge 224 ] && [ "$first_oct" -le 239 ];then
        continue
    fi
    ip_full="http://${ip_raw}/${stream}"
    # 读取模板替换，保留#EXTINF行
    sed "s|ipipip|${ip_full}|g" "template/template_${city}.txt" >> "${city_m3u_tmp}"
    # 附带速度行，用于后面全局排序
    echo "SPEED:${speed_line}" >> "${city_m3u_tmp}"
done

log_print "${city} M3U临时文件生成完成，等待全部城市跑完统一全局排序"
cleanup 0
