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

# 临时文件列表，运行结束统一清理
TMP_FILES=()

# ========= 清理函数：区分异常终止/正常结束 =========
cleanup() {
    rm -f "${TMP_FILES[@]}"
    echo "临时文件清理完成" | tee -a "${LOG_FILE}"
}
trap cleanup EXIT

# 去除http前缀工具函数，返回纯ip:port
strip_http(){
    local str="$1"
    str="${str#http://}"
    str="${str#https://}"
    echo "$str"
}

if [ $# -eq 0 ]; then
  echo "用法：$0 [城市编号] | $0 0 代表全部城市合并测速"
  exit 1
fi
city_choice="$1"

# 城市配置，和python采集脚本保持对应
case $city_choice in
    1)
        city="Beijing_dianxin"
        stream="udp/239.94.0.103:5140"
        run_single=1
        ;;
    2)
        city="Shanghai_dianxin"
        stream="udp/239.94.0.103:5140"
        run_single=1
        ;;
    3)
        city="Guangzhou_dianxin"
        stream="udp/239.94.0.103:5140"
        run_single=1
        ;;
    0)
        echo "===== 【GA控制台】开始多城市批量测速 =====" | tee -a "${LOG_FILE}"
        bash "$0" 1
        bash "$0" 2
        bash "$0" 3

        > tmp_merge_all.tmp
        TMP_FILES+=("tmp_merge_all.tmp")
        cat ip/tmp_speed_*.txt 2>/dev/null >> tmp_merge_all.tmp
        echo "【GA控制台】合并的测速总行数: $(wc -l < tmp_merge_all.tmp)" | tee -a "${LOG_FILE}"

        sort -k1 -nr tmp_merge_all.tmp | awk '!seen[$2]++' | head -n ${MERGE_TOP_COUNT} > tmp_sort_all.tmp
        TMP_FILES+=("tmp_sort_all.tmp")

        > zubo.txt
        while read -r raw_byte ip_raw; do
            ip_raw=$(strip_http "$ip_raw")
            ip_full="http://${ip_raw}"
            sed "s|ipipip|$ip_full|g" template/template_Beijing_dianxin.txt >> zubo.txt
        done < tmp_sort_all.tmp

        echo "✅ 全局合并完成，zubo.txt生成完毕，共 $(wc -l < zubo.txt) 行" | tee -a "${LOG_FILE}"
        exit 0
        ;;
    *)
        echo "❌ 无效城市编号"
        exit 1
        ;;
esac

# ========== 单城市测速流程 ==========
good_ip_file="ip/good_${city}_ip.txt"
echo "【GA控制台】城市 ${city}，待测文件路径: $good_ip_file" | tee -a "${LOG_FILE}"
if [ ! -f "$good_ip_file" ];then
    echo "❌ $good_ip_file 不存在，跳过城市 ${city}" | tee -a "${LOG_FILE}"
    exit 0
fi

# 打印文件行数，确认文件读取正常
file_line=$(wc -l < "$good_ip_file")
echo "【GA控制台】$good_ip_file 文件行数：$file_line" | tee -a "${LOG_FILE}"

# 清理空行，去重
sed '/^\s*$/d' "$good_ip_file" | sort | uniq > tmp_filter_ip.tmp
TMP_FILES+=("tmp_filter_ip.tmp")
mv tmp_filter_ip.tmp "$good_ip_file"
total_ip=$(wc -l < "$good_ip_file")
echo "===== 【GA控制台】城市 ${city}，去重后待测IP总数: $total_ip =====" | tee -a "${LOG_FILE}"

# 测速函数
speed_test(){
    local ip="$1"
    local url="http://$ip/$stream"
    echo "DEBUG: 测试地址 $url" | tee -a "${LOG_FILE}"
    local raw_speed=""
    for ((r=0; r<=SPEED_RETRY; r++)); do
        raw_speed=$(curl --connect-timeout 3 --max-time 10 -o /dev/null -w "%{speed_download}" "$url" 2>/dev/null)
        if [[ "$raw_speed" =~ ^[0-9\.]+$ && $(echo "$raw_speed > 0" | bc) -eq 1 ]];then
            break
        fi
    done

    if [[ "$raw_speed" =~ ^[0-9\.]+$ && $(echo "$raw_speed > 0" | bc) -eq 1 ]]; then
        echo "$raw_speed $ip"
        if (( $(echo "$raw_speed >= 1048576" | bc -l) )); then
            show_speed=$(echo "scale=2; $raw_speed / 1048576" | bc)M
        elif (( $(echo "$raw_speed >= 1024" | bc -l) )); then
            show_speed=$(echo "scale=2; $raw_speed / 1024" | bc)k
        else
            show_speed="${raw_speed}B"
        fi
        echo "【测速成功】$ip 速度：$show_speed" | tee -a "${LOG_FILE}"
    else
        echo "【测速失败】$ip raw_speed=$raw_speed" | tee -a "${LOG_FILE}"
    fi
}
export -f speed_test
export SPEED_RETRY stream LOG_FILE

> tmp_speed_result.tmp
TMP_FILES+=("tmp_speed_result.tmp")
echo "【GA控制台】准备启动并发测速，并发数：${SPEED_CONCURRENT}" | tee -a "${LOG_FILE}"

# 并发测速
cat "$good_ip_file" | xargs -P ${SPEED_CONCURRENT} -I {} bash -c 'speed_test "{}"' > tmp_speed_result.tmp

valid_line=$(wc -l < tmp_speed_result.tmp)
echo "【GA控制台】本次测速，拿到有效速度记录行数：$valid_line" | tee -a "${LOG_FILE}"

sort -k1 -nr tmp_speed_result.tmp > ip/tmp_speed_${city}.tmp
cp ip/tmp_speed_${city}.tmp "ip/tmp_speed_${city}.txt"
echo "===== ${city} 测速排序完成 =====" | tee -a "${LOG_FILE}"

# 生成城市独立txt
> tmp_city_out.tmp
TMP_FILES+=("tmp_city_out.tmp")
for ((n=1; n<=CITY_TOP_COUNT; n++));do
    ip_raw=$(awk -v line="$n" 'NR==line{print $2}' ip/tmp_speed_${city}.tmp)
    ip_raw=$(strip_http "$ip_raw")
    if [ -z "$ip_raw" ];then continue;fi
    ip_only=$(echo "$ip_raw" | cut -d: -f1)
    first_oct=$(echo "$ip_only" | cut -d. -f1)
    if [ "$first_oct" -ge 224 ] && [ "$first_oct" -le 239 ];then
        continue
    fi
    ip_full="http://${ip_raw}"
    sed "s|ipipip|$ip_full|g" template/template_${city}.txt >> tmp_city_out.tmp
done

sed 's|第[0-9]*\/[0-9]*个：||g' tmp_city_out.tmp | grep -v '///' > "txt/${city}.txt"
echo "✅ ${city} 已输出文件 txt/${city}.txt" | tee -a "${LOG_FILE}"
