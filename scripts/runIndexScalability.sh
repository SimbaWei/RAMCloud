echo "# Generated by 'clusterperf.py indexScalability'"
echo "#"
echo "# numIndexlets  throughput(klookups/sec)"
echo "#-----------------------------------------------"

# set TOTAL = total number of servers in config file
(( TOTAL=19 ))

# set MIN = minimum number of indexlets
(( MIN_INDEXLET=1 ))

# set MAX = maximum number of indexlets
(( MAX_INDEXLET=10 ))

for (( i=$MIN_INDEXLET; i<=$MAX_INDEXLET; i++ )) do

  # Number of servers required (excluding clients) for i indexlets
  (( SERVERS=i+4 ))

  # Currently clients are set to occupy the remaining servers. To increase the number
  # of clients, increase the number of hosts in config file.
  (( CLIENTS=TOTAL-SERVERS ))

  # run clusterperf on for i indexlets
  scripts/clusterperf.py -i $i -n $CLIENTS --servers=$SERVERS indexScalability > /dev/null

  # extract max thoroughput for i indexlets
  grep -v '^#' logs/latest/client0* | grep -v '/.' | awk -v var="$i" '$2>x{x=$2};END{print "\t"var"\t\t"  x}'

  # move latest dir to "i" dir in logs
  mv logs/latest logs/$i

done
