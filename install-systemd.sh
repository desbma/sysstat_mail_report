#!/bin/bash -e

install -Dm 755 -t /usr/local/bin ./sysstat_report.py

install -Dm 644 -T ./systemd/sysstat-report.conf /etc/conf.d/sysstat-report
install -Dm 644 -t /etc/systemd/system ./systemd/sysstat-report@.{service,timer}

echo 'Set configuration in /etc/conf.d/sysstat-report

Enable periodic reports with:
systemctl enable --now sysstat-report@daily.timer
systemctl enable --now sysstat-report@weekly.timer
systemctl enable --now sysstat-report@monthly.timer'
