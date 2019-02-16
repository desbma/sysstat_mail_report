Sysstat Mail Report
===================

Send daily/weekly/monthly email reports with graphs plotted from sysstat monitoring data.

There are a lot of tools available to plot sysstat data and generate graphs, but they either have important limitations, or require a web server running to serve the graphs.

This tool attempts to be simple and efficient and allow generating periodic reports (from cron) to be emailed directly.


## Features

* Allows generating daily/weekly/monthly reports
* Generates graphs with data from 5min load average, CPU usage, memory usage, swap usage, network IO, TCP/UDP socket stats (IPv4 & IPv6), TCP (IPv4) socket state transistions, and drive IO (see examples below), additionnaly display reboot times
* Construct email with both PNG and alternate ASCII graphs to be compatible with text only mail clients, or low bandwith mail viewing
* Automatically scale graphs according to system characteristics (eg. get total memory for memory graph y axis)
* Weekly and monthly graphs are automatically smoothed (hides small variations better viewed on daily graphs) to remain readable
* Properly handle special cases like DST time shifts, months with less than 30 days, etc
* Few dependencies: sysstat, gnuplot, sendmail and Python 3.4 (no Python package dependencies): install is as simple as copying a file on most servers. No server or daemon is required or installed.
* Graph generation is usually very fast, even with large data files, because all the data processing is done by Gnuplot
* Automatically crunch images to save a few KB per email without any loss of quality
* Optionally support SVG images for crisp looking graphs <sup>1</sup>

<sup>1. SVG rendering has been tested successfully in Thunderbird and Geary email clients, but is not supported by GMail (as of 2015/09/07), and probably many other older clients. In case of doubt, use the default PNG + text fallback mode.</sup> 


## Graph examples

Click images to see full size.

Daily CPU graph:  
[![Daily CPU graph](https://i.imgur.com/Pkh6VHum.png)](https://i.imgur.com/Pkh6VHu.png)

Daily memory graph:  
[![Daily memory graph](https://i.imgur.com/o0Qzd8nm.png)](https://i.imgur.com/o0Qzd8n.png)

Daily network graph:  
[![Daily network graph](https://i.imgur.com/yZ8zKEMm.png)](https://i.imgur.com/yZ8zKEM.png)

Daily IO graph:  
[![Daily IO graph](https://i.imgur.com/sCEZ773m.png)](https://i.imgur.com/sCEZ773.png)

Weekly network graph:  
[![Weekly network graph](https://i.imgur.com/pYRv26Em.png)](https://i.imgur.com/pYRv26E.png)


## Dependencies

* [Python >= 3.4](https://www.python.org/downloads/)
* [Gnuplot >= 4.6](http://www.gnuplot.info/)
* sendmail (configured and operational to send emails)
* [optipng](http://optipng.sourceforge.net/) (optional)

And of course [sysstat](http://sebastien.godard.pagesperso-orange.fr/).

On Ubuntu and other Debian derivatives, you can install all of them with:  
`sudo apt-get install sysstat python3 gnuplot-nox sendmail-bin optipng`


## Installation

Download it to `/usr/local/bin`, ie with:

    curl https://raw.githubusercontent.com/desbma/sysstat_mail_report/master/sysstat_report.py > /usr/local/bin/sysstat_report.py && chmod +x /usr/local/bin/sysstat_report.py


### Sysstat configuration

For the weekly and monthly reports to be generated properly, you may need to increase the value of `HISTORY` in `/etc/sysstat/sysstat`, to respectively at least 7 and 31.

Stat files compressed with gzip, bzip2 or xz are supported.

To enable the socket and tcp reports, you need to edit the `SADC_OPTIONS` variable in `/etc/sysstat/sysstat` to add `-S SNMP,IPV6`.


## Usage

### Cron

The simplest way of calling the script is through a cron job, so for example for a daily report, create the file `/etc/cron.daily/sysstat-report`, make it executable, and add the lines:

    #!/bin/sh
    exec sysstat_report.py daily 'Sysstat <email.from@example.com>' 'email.to@example.com'

When the script is called every day, you will receive an email with the graphs for the previous day.

### Systemd

To control `systat_report` with Systemd, unit files are provided, you can install them by running `./install-systemd.sh`.
Follow given instructions to configure and enable periodic reports.

### Command line options

Run `sysstat_report.py -h` to get full command line reference.


## License

[LGPLv2](https://www.gnu.org/licenses/old-licenses/lgpl-2.1-standalone.html)
