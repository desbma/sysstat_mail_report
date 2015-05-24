Sysstat Mail Report
===================

Send daily/weekly/monthly email reports with graphs plotted from sysstat monitoring data.

There are a lot of tools available to plot sysstat data and generate graphs, but they either have important limitations, or require a web server running to serve the graphs.

This tool attempts to be simple and efficient and allow generating periodic reports (from cron) to be emailed directly.


## Features

* Allow generating daily/weekly/monthly reports
* Generates graphs with data from CPU usage, memory usage, swap usage, network IO and drive IO (see examples below), additionnaly display reboot times
* Construct email with both PNG and alternate ASCII graphs to be compatible with text only mail clients, or low bandwith mail viewing
* Automatically scale graphs according to system characteristics (ie. get total memory for memory graph y axis)
* Weekly and monthly graphs are automatically smoothed (hides small variations better viewed on daily graphs) to remain readable
* Properly handle special cases like DST time shifts, months with less than 30 days, etc
* Few dependencies: sysstat, gnuplot, sendmail and Python 3.4 (no Python package dependencies): install is as simple as copying a file on most servers. No server or daemon is required or installed.
* Automatically crunch images to save a few KB per email without any loss of quality


## Graph examples

Click images to see full size.

Daily CPU graph:  
[![Daily CPU graph](http://i.imgur.com/qyTeBFsm.png)](http://i.imgur.com/qyTeBFs.png)

Daily memory graph:  
[![Daily memory graph](http://i.imgur.com/W3uvwtMm.png)](http://i.imgur.com/W3uvwtM.png)

Daily network graph:  
[![Daily network graph](http://i.imgur.com/yZ8zKEMm.png)](http://i.imgur.com/yZ8zKEM.png)

Daily IO graph:  
[![Daily IO graph](http://i.imgur.com/sCEZ773m.png)](http://i.imgur.com/sCEZ773.png)

Weekly network graph:  
[![Weekly network graph](http://i.imgur.com/pYRv26Em.png)](http://i.imgur.com/pYRv26E.png)


## Dependencies

* [Python >= 3.4](https://www.python.org/downloads/)
* [Gnuplot >= 4.6](http://www.gnuplot.info/)
* sendmail (configured and operational to send emails)
* [optipng](http://optipng.sourceforge.net/) (optional)

And of course [sysstat](http://sebastien.godard.pagesperso-orange.fr/).

On Ubuntu and other Debian derivatives, you can install all of them with:
`sudo apt-get install sysstat python3.4 gnuplot-nox sendmail-bin optipng`


## Installation

Download it to `/usr/local/bin`, ie with:

    curl https://raw.githubusercontent.com/desbma/sysstat_mail_report/master/sysstat_report.py > /usr/local/bin/sysstat_report.py && chmod +x /usr/local/bin/sysstat_report.py


### Sysstat configuration

For the weekly and monthly reports to be generated properly, you may need to increase the value of `HISTORY` in `/etc/sysstat/sysstat`, to respectively at least 7 and 31.

Stat files compressed with bzip2 are handled properly.


## Usage

The recommand way of calling the script is through a cron job, so for example for a daily report, create the file `/etc/cron.daily/sysstat-report`, make it executable, and add the lines:

    #!/bin/sh
    sysstat_report.py daily 'Sysstat <email.from@example.com>' 'email.to@example.com' -v warning

When the script is called every day, you will receive an email with 5 graphs for the previous day.

Run `sysstat_report.py -h` to get full command line reference.


## License

[GPLv3](https://www.gnu.org/licenses/gpl-3.0-standalone.html)
