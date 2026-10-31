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
        rm -f tmp_good_ip zubo_*.tmp tmp_all.txt tmp_speed_list.tmp
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
        stream_group="239.94.0.103:5140"
        ;;
    2)
        city="Shanghai_dianxin"
        stream_group="替换上海组播地址"
        ;;
    3)
        city="Guangzhou_dianxin"
        stream_group="替换广州组播地址"
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
        tmp_speed_list="tmp_speed_list.tmp"
        > "$tmp_speed_list"

        # 读取临时文件：速度,裸IP,组播地址（全部不带http）
        cat txt/*.txt | awk '
        BEGIN{speed_val=0;ip_addr="";group_addr=""}
        /^SPEED:/{
            speed_raw=substr($0,8);
            if(speed_raw ~ /M/){sub(/M/,"",speed_raw); speed_val=speed_raw*1024}
            else if(speed_raw ~ /k/){sub(/k/,"",speed_raw); speed_val=speed_raw}
            else{speed_val=0}
            next;
        }
        /^IP:/{
            ip_addr=substr($0,4);
            next;
        }
        /^GROUP:/{
            group_addr=substr($0,7);
            split(ip_addr,ipp,":");
            split(ipp[1],oct,".");
            #过滤组播IP
            if(oct[1]>=224 && oct[1]<=239) {speed_val=0;ip_addr="";group_addr="";next;}
            if(speed_val>0){
                print speed_val,ip_addr,group_addr
            }
            speed_val=0;ip_addr="";group_addr=""
        }
        ' | sort -k1 -nr > "${tmp_speed_list}"

        log_print "===== 全局测速排名预览【裸IP+速度，无http】 ====="
        cat "${tmp_speed_list}" | awk '{printf "%-22s %s\n",$2,$1}' | tee -a "${LOG_FILE}"

        # 生成最终m3u文件：**在这里才一次性添加http://**
        while read -r sp_num ip grp;do
            [ -z "$ip" ] && continue
            # 拼接完整url，只加一次http://
            full_url="http://${ip}/rtp/${grp}"
            # 读取模板，模板内绝对不要写http://！！！
            sed "s|ipipip|${full_url}|g" "template/template_${city}.txt" >> "${merge_output}"
        done < "${tmp_speed_list}"

        # 复制输出文件
        cp "$merge_output" zubo.m3u
        cp "$merge_output" txt/Beijing_dianxin.m3u
        cp "$merge_output" txt/Shanghai_dianxin.m3u
        cp "$merge_output" txt/Guangzhou_dianxin.m3u
        cp "$merge_output" 组播_Beijing_dianxin.m3u
        cp "$merge_output" 组播_Shanghai_dianxin.m3u
        cp "$merge_output" 组播_Guangzhou_dianxin.m3u

        # 文件行数校验
        log_print "==== 文件生成校验 ===="
        log_print "zubo.m3u 行数：$(wc -l < zubo.m3u)"
        log_print "all_city_sorted.m3u 行数：$(wc -l < ${merge_output})"
        log_print "组播_Beijing_dianxin.m3u 行数：$(wc -l < 组播_Beijing_dianxin.m3u)"
        log_print "======================"
        log_print "✅ 全局排序完成！保留M3U格式，按测速从快到慢排序"
        log_print "👉 zubo.m3u / txt/下城市文件 / 组播_*.m3u 内容完全相同"

        log_print "===== 全局M3U排序列表预览 ====="
        cat "${merge_output}" | tee -a "${LOG_FILE}"
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
speed_log="speedtest_${city}_${time_tag}.log"
city_tmp="txt/${city}.txt"
> "$speed_log"
> "$city_tmp"

log_print "======== 开始测速 ${city} ========"
log_print "读取存活IP文件: ${good_ip}"

if [ ! -f "$good_ip" ];then
    log_print "❌ 文件不存在：$good_ip，跳过该城市"
    cleanup 0
fi

sort "$good_ip" | uniq | sed '/^\s*$/d' > tmp_filter_ip && mv tmp_filter_ip "$good_ip"
line_count=$(wc -l < "$good_ip")
log_print "存活IP数量：$line_count"

if [ "$line_count" -eq 0 ];then
    log_print "⚠️ IP文件为空，跳过"
    cleanup 0
fi

log_print "开始并发测速，并发数：${SPEED_CONCURRENT}"

speed_test(){
    local ip="$1"
    local idx="$2"
    local total="$3"
    # 测速用完整url，仅curl测速使用
    local curl_url="http://${ip}/rtp/${stream_group}"
    local raw_speed=""
    for ((r=0; r<=SPEED_RETRY; r++)); do
        raw_speed=$(curl --connect-timeout 3 --max-time 15 -o /dev/null -w "%{speed_download}" "$curl_url" 2>/dev/null)
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
        # 控制台输出：只打印IP:端口 + 速度，不带http！！
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
# 速度排序
awk '
{
    ip=$2
    speed=$1
    if(speed ~ /M/){num=speed*1024}
    else if(speed ~ /k/){num=speed}
    else {num=0}
    print num,speed,ip
}
' "$speed_log" | sort -k1 -nr > result_sort.tmp

# 写入城市临时文件：只存标记、裸IP、组播地址，**无http**
for ((n=1; n<=CITY_TOP_COUNT; n++));do
    ip_raw=$(awk -v line="$n" 'NR==line{print $3}' result_sort.tmp)
    speed_line=$(awk -v line="$n" 'NR==line{print $2}' result_sort.tmp)
    ip_raw=$(strip_http "$ip_raw")
    if [ -z "$ip_raw" ];then
        continue
    fi
    ip_only=$(echo "$ip_raw" | cut -d: -f1)
    first_oct=$(echo "$ip_only" | cut -d. -f1)
    if [ -n "$first_oct" ] && [ "$first_oct" -ge 224 ] && [ "$first_oct" -le 239 ];then
        continue
    fi
    # 临时文件保存原始数据，不带任何http
    echo "SPEED:${speed_line}" >> "${city_tmp}"
    echo "IP:${ip_raw}" >> "${city_tmp}"
    echo "GROUP:${stream_group}" >> "${city_tmp}"
done
rm -f result_sort.tmp

log_print "${city} 临时数据保存完成，等待全部城市跑完统一全局排序"
cleanup 0
