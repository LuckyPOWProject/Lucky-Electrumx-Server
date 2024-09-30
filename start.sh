export COIN=Luckycoin;
export DB_DIRECTORY=dbpath;
#export DB_ENGINE=rocksdb;
export DAEMON_URL=deamonauth;
#export SERVICES=tcp://:50001,ws://:50003
export SERVICES=tcp://0.0.0.0:50010,ws://0.0.0.0:50003;
export LOG_LEVEL=info;
export COST_SOFT_LIMIT=0;
export COST_HARD_LIMIT=0;
export INITIAL_CONCURRENT=500;

python3 electrumx_server