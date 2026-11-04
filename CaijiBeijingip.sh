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
MIN_SPEED_KB=30         # >=30k 才保留写入zubo.txt，B单位全部丢弃
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

# 辅助函数：把速度字符串转为KB数值（修复小数兼容，B类型返回0）
speed2kb(){
    local s="$1"
    if [[ $s == *M ]];then
        val=${s//M/}
        echo "$val * 1024" | bc -l
    elif [[ $s == *k ]];then
        val=${s//k/}
        echo "$val" | bc -l
    else
        # B单位直接返回0
        echo "0"
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

# ===================== 全局合并部分（修复清空zubo致命BUG + 增加批量模式加密）=====================
if [ "$city_choice" -eq 0 ];then
    > zubo_merge_raw.txt

    # 城市列表，和上面case里的城市一一对应
    city_arr=(
        "Beijing_dianxin"
        "Shanghai_dianxin"
        "Guangzhou_dianxin"
    )

    for city_name in "${city_arr[@]}"; do
        speed_file="ip/tmp_speed_${city_name}.txt"
        template_file="template/template_${city_name}.txt"

        log_print "==== 正在处理城市：${city_name} ===="
        log_print "测速文件：$speed_file  模板文件：$template_file"

        # 判断文件是否存在，并且文件不为空
        if [ ! -f "$speed_file" ] || [ ! -s "$speed_file" ] || [ ! -f "$template_file" ];then
            log_print "⚠️ 跳过【${city_name}】，测速文件/模板缺失或者测速结果为空"
            continue
        fi

        # 过滤：只保留 >=MIN_SPEED_KB 的IP，再按速度降序、去重、取CITY_TOP_COUNT条
        awk -v minkb="$MIN_SPEED_KB" '
        function getnum(s){
            if(s ~ /M/){sub(/M/,"",s); return s*1024}
            if(s ~ /k/){sub(/k/,"",s); return s+0}
            return 0
        }
        {
            speed_val=$1
            ip_port=$2
            num=getnum(speed_val)
            if(num >= minkb) print num, ip_port
        }' "$speed_file" | sort -k1 -nr | awk -v top="$CITY_TOP_COUNT" '
        BEGIN{count=0}
        !seen[$2]++{
            if(count < top){
                print $2
                count++
            }
        }' > tmp_city_ip

        # 过滤组播IP
        awk '
        {
            split($1,iparr,":");
            split(iparr[1],oct,".");
            if(oct[1]>=224 && oct[1]<=239) next;
            print $1
        }' tmp_city_ip > tmp_city_ip_filtered

        # 读取当前城市IP，只套当前城市模板写入临时合并文件 zubo_merge_raw.txt
        while read -r ip_raw; do
            ip_full="http://${ip_raw}"
            sed "s|ipipip|$ip_full|g" "$template_file" >> zubo_merge_raw.txt
        done < tmp_city_ip_filtered

        rm -f tmp_city_ip tmp_city_ip_filtered
    done

    log_print "✅ 全部城市频道合并完成，开始全局速度排序，保留前${MERGE_TOP_COUNT}条"

    # 提取速度，对zubo_merge_raw.txt进行全局排序，输出最终zubo.txt
    > zubo.txt
    awk '
    /#EXTINF:-1/ {
        getline ip_line
        sub(/http:\/\//,"",ip_line)
        split(ip_line,ipp,":")
        ip=ipp[1]
        next
    }
    /speed=/{
        sub(/.*speed=/,"")
        sub(/,.*/,"")
        sp=$0
        print sp,ip
    }' zubo_merge_raw.txt | sort -k1 -nr | head -n ${MERGE_TOP_COUNT} > zubo_rank.tmp

    # 重建最终zubo.txt，从原始合并文件读取内容，不再读取已清空的zubo.txt
    while read -r sp ip;do
        grep "#EXTINF.*${ip}" zubo_merge_raw.txt >> zubo.txt
    done < zubo_rank.tmp

    rm -f zubo_merge_raw.txt zubo_rank.tmp

    log_print "✅ 全局合并完成，zubo.txt 使用#EXTINF模板格式，保留前${MERGE_TOP_COUNT}条"
    log_print "==================== 全局合并完成 ===================="

    # =========【批量模式 加密zubo.txt生成zubo.enc】=========
    if [ -f "zubo.txt" ] && [ -s "zubo.txt" ]; then
        log_print "检测到非空zubo.txt，开始加密生成 zubo.enc"
        openssl enc -aes-256-cbc -salt -in zubo.txt -out zubo.enc -k "$ZUBO_PASSWORD"
    else
        log_print "zubo.txt 文件为空或不存在，跳过加密，不生成zubo.enc"
    fi
    # ======================================================

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

# =====================【新增防护代码，防止读取旧IP文件】=====================
# 获取脚本启动时间，判断good_ip文件是否是本次python扫描新生成
script_start_epoch=$(date +%s)
file_mtime=$(stat -c %Y "${good_ip}")
time_diff=$(( script_start_epoch - file_mtime ))
# 如果文件修改时间早于60秒前，判定为旧文件（python本次没有成功更新）
if [ ${time_diff} -gt 60 ];then
    log_print "❌ 警告！${good_ip} 是旧文件（${time_diff}秒前生成），Python扫描未成功刷新，为避免测速旧IP，直接跳过该城市！"
    cleanup 0
fi
# ==========================================================================

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

        # ==========【新增阈值判断】小于30k直接跳过，不写入speed_log，不打印✅成功日志 ==========
        kb_val=$(speed2kb "$a")
        if (( $(echo "$kb_val < $MIN_SPEED_KB" | bc -l) ));then
            # 低于阈值，仅保留【正在测速】，不输出成功记录、不进结果
            return 0
        fi
        # >=30k 才输出到测速结果文件
        echo "$a $ip"
        # 成功日志，stderr输出，仅控制台展示
        echo "✅ 测速成功 $ip 速度: $a" >&2
    else
        # 测速失败日志，stderr输出，仅控制台展示
        echo "❌ 测速失败 $ip raw_speed:$raw_speed" >&2
    fi
}
export -f speed_test
export SPEED_RETRY stream MIN_SPEED_KB

# 并发测速
cat "$good_ip" | xargs -P ${SPEED_CONCURRENT} -I {} bash -c 'speed_test "{}"' > $speed_log

log_print "测速完成，开始按速度降序排序"
# ============【已修复：过滤B单位 + 过滤小于MIN_SPEED_KB的行，存入result_ip ============
awk -v minkb="$MIN_SPEED_KB" '
function tokb(s){
    if(s ~ /M$/){sub(/M/,"",s); return s*1024}
    if(s ~ /k$/){sub(/k/,"",s); return s+0}
    return 0
}
{
    kb=tokb($1);
    if($1 !~ /B$/ && kb >= minkb){
        print kb "\t" $0
    }
}
' "$speed_log" | sort -k1,1nr | cut -f2- > "$result_ip"
# ======================================================

# 保存tmp_speed给全局合并使用
cp "$result_ip" "ip/tmp_speed_${city}.txt"

log_print "========= ${city} 测速排序结果 ========="
cat "${result_ip}" | tee -a "${LOG_FILE}"
log_print "========================================"

# 生成城市txt文件，使用模板；只保留≥MIN_SPEED_KB的IP写入txt，B全部丢弃
> tmp_all.txt
count_valid=0
while read -r line; do
    sp_str=$(echo $line | awk '{print $1}')
    ip_raw=$(echo $line | awk '{print $2}')
    kb_val=$(speed2kb "$sp_str")
    # 低于阈值直接跳过，不会写入城市txt
    if (( $(echo "$kb_val < $MIN_SPEED_KB" | bc -l) ));then
        continue
    fi
    # 最多取CITY_TOP_COUNT条
    if (( count_valid >= CITY_TOP_COUNT ));then
        break
    fi

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
    ((count_valid++))
done < "$result_ip"

# 输出txt文件
sed 's|第[0-9]*\/[0-9]*个：||g' tmp_all.txt | grep -v '///' > "txt/${city}.txt"
rm -f tmp_all.txt $speed_log $result_ip
log_print "${city} 测速完成，文件输出至 txt/${city}.txt，仅保留 >=${MIN_SPEED_KB}k 有效IP，B单位全部过滤"

cleanup 0

# 单城市模式：加密明文zubo.txt，输出 zubo.enc，和网页解密算法匹配
if [ -f "zubo.txt" ] && [ -s "zubo.txt" ]; then
    log_print "检测到zubo.txt，开始加密生成 zubo.enc"
    openssl enc -aes-256-cbc -salt -in zubo.txt -out zubo.enc -k "$ZUBO_PASSWORD"
else
    log_print "zubo.txt 文件为空或不存在，跳过加密，不生成zubo.enc"
fi
