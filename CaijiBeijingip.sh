#!/bin/bash

# ====================== 全局可配置参数（GA专用）======================
NC_CONCURRENT=12        # 端口探测并发，GA建议 8~12
SPEED_CONCURRENT=6      # 测速并发
PROGRESS_STEP=20        # 每处理多少个IP打印一次进度
NC_RETRY=1              # 端口探测失败重试次数
SPEED_RETRY=1           # 测速重试次数
CITY_TOP_COUNT=30       # 单个城市结果保留前N条
MERGE_TOP_COUNT=100     # 合并全部城市后保留前N条
SAVE_LOG=1              # 1=保存日志，0=关闭日志保存
CURL_TIMEOUT=8          # curl测速最大时长
MIN_SPEED=100           # 最低有效速度，>=100k 才判定可用
# ====================================================================

LOG_DIR="logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/scan_$(date +%Y%m%d_%H%M%S).log"

# 清理函数
cleanup() {
    local exit_type="$1"
    if [ "$exit_type" -eq 1 ];then
        echo -e "\n⚠️  检测到脚本异常退出，清理临时文件..." | tee -a "${LOG_FILE}"
        rm -f tmp_good_ip zubo_*.tmp tmp_all.txt "${TMP_FILE_LIST[@]}"
        exit 1
    else
        rm -f tmp_good_ip zubo_*.tmp tmp_all.txt
        log_print "✅ 当前城市临时文件清理完毕"
    fi
}
trap 'cleanup 1' SIGINT SIGTERM SIGHUP

log_print(){
    if [ "$SAVE_LOG" -eq 1 ];then
        echo "$1" | tee -a "${LOG_FILE}"
    else
        echo "$1"
    fi
}

strip_http(){
    local str="$1"
    str="${str#http://}"
    str="${str#https://}"
    echo "$str"
}

if [ $# -eq 0 ]; then
  log_print "开始多城市测速"
  log_print "0=全部城市  1:北京  2:上海 3:广州"
  read -t 5 -p "超时默认全部城市(0)" city_choice
  [ -z "$city_choice" ] && city_choice=0
else
  city_choice=$1
fi

mkdir -p ip txt template

case $city_choice in
    1)
        city="Beijing_dianxin"
        stream="rtp/233.50.201.136:5140"
        ;;
    2)
        city="Shanghai_dianxin"
        stream="rtp/233.18.204.67:5140"
        ;;
    3)
        city="Guangzhou_dianxin"
        stream="rtp/替换广州组播地址"
        ;;
    0)
        # 批量执行所有城市
        for option in 1 2 3; do
          bash "$0" "$option"
        done
        log_print "==================== 所有城市测速完成，开始全局合并 ===================="
        > zubo_merge_raw.txt
        city_arr=("Beijing_dianxin" "Shanghai_dianxin" "Guangzhou_dianxin")
        for city_name in "${city_arr[@]}"; do
            speed_file="ip/tmp_speed_${city_name}.txt"
            template_file="template/template_${city_name}.txt"
            log_print "==== 正在处理城市：${city_name} ===="
            if [ ! -f "$speed_file" ] || [ ! -s "$speed_file" ] || [ ! -f "$template_file" ];then
                log_print "⚠️ 跳过【${city_name}】测速文件为空"
                continue
            fi
            # 只保留 >=MIN_SPEED k 的有效IP
            awk -v min="$MIN_SPEED" '
            {
                sp=$1;ip=$2
                sub(/k/,"",sp)
                if(sp >= min) print sp,ip
            }' "$speed_file" | sort -k1 -nr | head -n ${CITY_TOP_COUNT} > tmp_valid_ip

            while read -r sp ip_raw; do
                ip_full="http://${ip_raw}/${stream}"
                sed "s|ipipip|$ip_full|g" "$template_file" >> zubo_merge_raw.txt
            done < tmp_valid_ip
            rm -f tmp_valid_ip
        done
        # 全局合并取前MERGE_TOP_COUNT
        head -n ${MERGE_TOP_COUNT} zubo_merge_raw.txt > zubo.txt
        rm -f zubo_merge_raw.txt
        log_print "✅ zubo.txt 合并完成，仅保留 >=${MIN_SPEED}k 的IP"
        exit 0
        ;;
    *)
        log_print "⚠️ 城市编号不存在"
        cleanup 1
        ;;
esac

# ========== 单个城市测速逻辑 ==========
log_print "======== 开始处理城市：${city} ========"
# ========== 这里修改文件名，匹配扫描输出 ==========
raw_ip="ip/good_${city}_ip.txt"
speed_out="ip/tmp_speed_${city}.txt"
> "$speed_out"

if [ ! -f "$raw_ip" ];then
    log_print "❌ $raw_ip 不存在"
    cleanup 0
fi

speed_test(){
    local ip="$1"
    local url="http://${ip}/${stream}"
    local tmpfile="/tmp/speed_${ip//./_}.tmp"
    rm -f "$tmpfile"

    echo "🔍 正在测速: $ip" >&2
    # curl下载，统计平均下载速度 单位B/s
    local speed_bps=$(curl --connect-timeout 2 --max-time ${CURL_TIMEOUT} -o "$tmpfile" -w "%{speed_download}" "$url" 2>/dev/null)
    rm -f "$tmpfile"

    # 转成 KB/s
    local speed_kb=$(echo "scale=0; $speed_bps / 1024" | bc)
    if (( speed_kb >= MIN_SPEED ));then
        echo "${speed_kb}k $ip"
        echo "✅ 测速成功 $ip  ${speed_kb}k" >&2
    else
        echo "$speed_kb $ip"
        echo "❌ 测速丢弃 $ip  $speed_kb" >&2
    fi
}
export -f speed_test

log_print "开始并发测速，并发数：${SPEED_CONCURRENT}"
cat "$raw_ip" | xargs -P${SPEED_CONCURRENT} -I {} bash -c 'speed_test "{}"' > "$speed_out"

log_print "测速完成，生成城市txt文件"
> tmp_all.txt
# 筛选有效IP，按速度降序，取CITY_TOP_COUNT条
awk -v min="$MIN_SPEED" '
{
    sp=$1;ip=$2
    sub(/k/,"",sp)
    if(sp >= min) print $1,$2
}' "$speed_out" | sort -k1 -nr | head -n ${CITY_TOP_COUNT} > tmp_top_valid

while read -r sp ip_raw; do
    ip_full="http://${ip_raw}/${stream}"
    sed "s|ipipip|$ip_full|g" template/template_${city}.txt >> tmp_all.txt
done < tmp_top_valid

sed 's|第[0-9]*\/[0-9]*个：||g' tmp_all.txt | grep -v '///' > "txt/${city}.txt"
rm -f tmp_all.txt tmp_top_valid
log_print "✅ ${city}.txt 输出完成，只保留 >=${MIN_SPEED}k 可播放IP"
cleanup 0
