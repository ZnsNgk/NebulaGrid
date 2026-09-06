echo "restarting api..."
sudo systemctl restart nebulagrid-api
echo "restarting scheduler..."
sudo systemctl restart nebulagrid-scheduler
echo "restarting node monitor..."
sudo systemctl restart nebulagrid-node-monitor
echo "restarting task executor..."
sudo systemctl restart nebulagrid-task-executor
echo "restarting runtime guard..."
sudo systemctl restart nebulagrid-runtime-guard
echo "restarting env install worker..."
sudo systemctl restart nebulagrid-env-install-worker
echo "restarting nginx..."
sudo service nginx restart
echo "All done!"
