#!/bin/bash

#pwd
if [ $# -eq 0 ]; then
  echo "开始测试······"
  echo "在5秒内输入1~4可选择城市"
  echo "请选择城市："
  echo "1. 北京（Beijing_dianxin）"
  read -t 5 -p "超时未输入,将按默认设置测试" city_choice

  if [ -z "$city_choice" ]; then
      echo "未检测到输入,默认测试全部"
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
        stream="rtp/239.94.0.103:5140"
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
echo "======== 开始检索 ${city} ========"

echo "从 '${ipfile}' 读取ip并添加到检测列表"
cat $ipfile >> tmp_ipfile
sort tmp_ipfile | uniq | sed '/^\s*$/d' > $ipfile
rm -f tmp_ipfile

while IFS= read -r ip; do
    # 尝试连接 IP 地址和端口号
    tmp_ip=$(echo -n "$ip" | sed 's/:/ /')
    output=$(nc -w 1 -v -z $tmp_ip 2>&1)
    # 如果连接成功，且输出包含 "succeeded"
    if [[ $output == *"succeeded"* ]]; then
        echo "$output" | grep "succeeded" | awk -v ip="$ip" '{print ip}' >> $good_ip
    fi
done < $ipfile

lines=$(wc -l < $good_ip)
echo "连接成功 $lines 个,开始测速······"
i=0
while read line; do
    i=$((i + 1))
    ip=$line
    url="http://$ip/$stream"
    curl $url --connect-timeout 5 --max-time 60 -o /dev/null >zubo.tmp 2>&1
    a=$(head -n 3 zubo.tmp | awk '{print $NF}' | tail -n 1)
    echo "第$i/$lines个：$ip    $a"
    echo "$ip    $a" >> speedtest_${city}_$time.log
done < $good_ip

echo "测速结果排序"
# 转换k/M为数值用于排序：k=千，M=兆，统一转成KB排序
awk '
{
    val=$1;ip=$2
    if(val ~ /M/){num=val*1024}
    else if(val ~ /k/){num=val}
    else {num=0}
    print num,val,ip
}
' speedtest_${city}_$time.log | sort -k1 -nr | awk '{print $2"  "$3}' > $result_ip

# ========== 精简：循环提取前30IP，不再写30次sed ==========
> tmp_all.txt
for n in {1..30};do
    ip=$(awk -v line="$n" 'NR==line{print $2}' $result_ip)
    if [ -z "$ip" ];then continue;fi
    sed "s/ipipip/$ip/g" template/template_${city}.txt >> tmp_all.txt
done

grep -vE '/{3}' tmp_all.txt > "txt/${city}.txt"
rm -f tmp_all.txt zubo.tmp $ipfile $good_ip speedtest_${city}_$time.log $result_ip
echo "${city} 测试完成，生成可用文件：'txt/${city}.txt'"


#--------合并所有城市txt文件 + 按M/k带宽降序排序，保留前100条--------
merge_output="txt/all_city_sorted.txt"
> $merge_output

# 提取速率、换算单位、降序，取前100行
cat txt/*.txt | awk '
function getnum(s){
    if(s ~ /M/){return s*1024}
    if(s ~ /k/){return s}
    return 0
}
{
    # 匹配 x.xM / xk 带宽字段
    for(i=1;i<=NF;i++){
        if($i ~ /^[0-9]+\.?[0-9]*[kM]$/){
            rate=$i
            break
        }
    }
    num=getnum(rate)
    print num,$0
}' | sort -k1 -nr | cut -d' ' -f2- | head -n 100 > $merge_output

echo "✅ 全部城市合并完成，按带宽降序，只保留最快前100条：$merge_output"
