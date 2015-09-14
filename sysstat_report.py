#!/usr/bin/env python3

""" Generate and send a sysstat mail report. """

import argparse
import bz2
import calendar
import contextlib
import datetime
import email.mime.image
import email.mime.multipart
import email.mime.text
import email.utils
import enum
import gzip
import inspect
import io
import itertools
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree


ReportType = enum.Enum("ReportType", ("DAILY", "WEEKLY", "MONTHLY"))
SysstatDataType = enum.Enum("SysstatDataType", ("LOAD", "CPU", "MEM", "SWAP", "NET", "IO"))
GraphFormat = enum.Enum("GraphFormat", ("TXT", "PNG", "SVG"))

HAS_OPTIPNG = shutil.which("optipng") is not None


def get_total_memory_mb():
  """ Return total amount of system RAM in MB. """
  output = subprocess.check_output(("free", "-m"), universal_newlines=True)
  output = output.splitlines()
  mem_line = next(itertools.dropwhile(lambda x: not x.startswith("Mem:"), output))
  total_mem = int(tuple(filter(None, map(str.strip, mem_line.split(" "))))[1])
  logging.getLogger().info("Total amount of memory: %u MB" % (total_mem))
  return total_mem


def get_max_network_speed():
  """ Get maximum Ethernet network interface speed in Mb/s. """
  max_speed = -1
  interfaces = os.listdir("/sys/class/net")
  assert(len(interfaces) > 1)
  for interface in interfaces:
    if interface == "lo":
      continue
    filepath = "/sys/class/net/%s/speed" % (interface)
    with open(filepath, "rt") as f:
      new_speed = int(f.read())
    logging.getLogger().debug("Speed of interface %s: %u Mb/s" % (interface, new_speed))
    max_speed = max(max_speed, new_speed)
  logging.getLogger().info("Maximum interface speed: %u Mb/s" % (max_speed))
  return max_speed


def get_reboot_times():
  """ Return a list of datetime.datetime representing machine reboot times. """
  reboot_times = []
  for i in range(1, -1, -1):
    log_filepath = "/var/log/wtmp%s" % (".%u" % (i) if i != 0 else "")
    if os.path.isfile(log_filepath):
      cmd = ("last", "-R", "reboot", "-f", log_filepath)
      output = subprocess.check_output(cmd, universal_newlines=True)
      output = output.splitlines()[0:-2]
      date_regex = re.compile(".*boot\s*(.*) - .*$")
      for l in output:
        date_str = date_regex.match(l).group(1).strip()
        # TODO remove fixed year
        date = datetime.datetime.strptime(date_str + " %u" % (datetime.date.today().year), "%a %b %d %H:%M %Y")
        reboot_times.append(date)
  return reboot_times


def minify_svg(svg_filepath):
  """ Open a SVG file, and return its minified content as a string. """
  xml.etree.ElementTree.register_namespace("", "http://www.w3.org/2000/svg")
  tree = xml.etree.ElementTree.parse(svg_filepath)
  root = tree.getroot()
  ns = root.tag.rsplit("}", 1)[0][1:]
  # remove title tags
  for e in root.findall(".//{%s}title/.." % (ns)):
    for se in e.findall("{%s}title" % (ns)):
      e.remove(se)
  with io.StringIO() as tmp:
    tree.write(tmp, encoding="unicode", xml_declaration=False)
    tmp.seek(0)
    data = "".join(map(str.strip, tmp.readlines()))
  return data


def format_email(exp, dest, subject, header_text, img_format, img_filepaths, alternate_text_filepaths):
  """ Format a MIME email with attached images and alternate text, and return email code. """
  assert(img_format in (GraphFormat.PNG, GraphFormat.SVG))

  msg = email.mime.multipart.MIMEMultipart("related")
  msg["Subject"] = subject
  msg["From"] = exp
  msg["To"] = dest

  # html
  html = ["<html><head></head><body>"]
  if header_text is not None:
    html.append("<pre>%s</pre><br>" % (header_text))
  if img_format is GraphFormat.PNG:
    html.append("<br>".join("<img src=\"cid:img%u\">" % (i) for i in range(len(img_filepaths))))
  elif img_format is GraphFormat.SVG:
    for img_filepath in img_filepaths:
      if img_filepath is not img_filepath[0]:
        html.append("<br>")
      data = minify_svg(img_filepath)
      html.append(data)
  html = "".join(html)
  html = email.mime.text.MIMEText(html, "html")

  # alternate text
  alternate_texts = []
  for alternate_text_filepath in alternate_text_filepaths:
    with open(alternate_text_filepath, "rt") as alternate_text_file:
      alternate_texts.append(alternate_text_file.read())
  if header_text is not None:
    text = "%s\n" % (header_text)
  else:
    text = ""
  text += "\n".join(alternate_texts)
  text = email.mime.text.MIMEText(text)

  msg_alt = email.mime.multipart.MIMEMultipart("alternative")
  msg_alt.attach(text)
  msg_alt.attach(html)
  msg.attach(msg_alt)

  if img_format is GraphFormat.PNG:
    for i, img_filepath in enumerate(img_filepaths):
      with open(img_filepath, "rb") as img_file:
        msg_img = email.mime.image.MIMEImage(img_file.read())
      msg_img.add_header("Content-ID", "<img%u>" % (i))
      msg.attach(msg_img)

  return msg.as_string()


def decompress(in_filepath, out_filepath):
  """ Decompress gzip or bzip2 input file to output file. """
  logging.getLogger().debug("Decompressing '%s' to '%s'..." % (in_filepath, out_filepath))
  with contextlib.ExitStack() as cm:
    if os.path.splitext(in_filepath)[1].lower() == ".gz":
      in_file = cm.enter_context(gzip.open(in_filepath, "rb"))
    elif os.path.splitext(in_filepath)[1].lower() == ".bz2":
      in_file = cm.enter_context(bz2.open(in_filepath, "rb"))
    out_file = cm.enter_context(open(out_filepath, "wb"))
    shutil.copyfileobj(in_file, out_file)


class SysstatData:

  """ Source of system stats. """

  def __init__(self, report_type, temp_dir):
    assert(report_type in ReportType)
    self.sa_filepaths = []
    today = datetime.date.today()

    if report_type is ReportType.DAILY:
      date = today - datetime.timedelta(days=1)
      self.sa_filepaths.append("/var/log/sysstat/sa%02u" % (date.day))

    elif report_type is ReportType.WEEKLY:
      for i in range(7, 0, -1):
        date = today - datetime.timedelta(days=i)
        filepath = date.strftime("/var/log/sysstat/%Y%m/sa%d")
        compressed_filepaths = ("%s.gz" % (filepath), "%s.bz2" % (filepath))
        if not os.path.isfile(filepath):
          for compressed_filepath in compressed_filepaths:
            if os.path.isfile(compressed_filepath):
              filepath = os.path.join(temp_dir, os.path.basename(filepath))
              decompress(compressed_filepath, filepath)
              break
        self.sa_filepaths.append(filepath)

    elif report_type is ReportType.MONTHLY:
      if today.month == 1:
        year = today.year - 1
        month = 12
      else:
        year = today.year
        month = today.month - 1
      for day in range(1, calendar.monthrange(year, month)[1] + 1):
        filepath = "/var/log/sysstat/%04u%02u/sa%02u" % (year, month, day)
        compressed_filepaths = ("%s.gz" % (filepath), "%s.bz2" % (filepath))
        if not os.path.isfile(filepath):
          for compressed_filepath in compressed_filepaths:
            if os.path.isfile(compressed_filepath):
              filepath = os.path.join(temp_dir, os.path.basename(filepath))
              decompress(compressed_filepath, filepath)
              break
        self.sa_filepaths.append(filepath)

  def generateData(self, dtype, output_filepath):
    """
    Generate data to plot (';' separated values).

    Return indexes of columns to use in output, and a dictionary of name -> filepath output datafiles if the provided
    output file had to be split.
    """
    assert(dtype in SysstatDataType)
    net_output_filepaths = {}

    for sa_filepath in self.sa_filepaths:
      cmd = ["sadf", "-d", "-U", "--"]
      dtype_cmd = {SysstatDataType.LOAD: ("-q",),
                   SysstatDataType.CPU: ("-u",),
                   SysstatDataType.MEM: ("-r",),
                   SysstatDataType.SWAP: ("-S",),
                   SysstatDataType.NET: ("-n", "DEV"),
                   SysstatDataType.IO: ("-b",)}
      cmd.extend(dtype_cmd[dtype])
      cmd.append(sa_filepath)
      with open(output_filepath, "ab") as output_file:
        subprocess.check_call(cmd, stdout=output_file)

    if dtype is SysstatDataType.NET:
      # split file by interface
      with open(output_filepath, "rt") as output_file:
        # skip first line(s)
        next(itertools.dropwhile(lambda x: not x.startswith("#"), output_file))
        next(output_file)
        for line in output_file:
          itf = line.split(";", 5)[3]
          if itf in net_output_filepaths:
            # not a new interface
            break
          base_filename, ext = os.path.splitext(output_filepath)
          net_output_filepaths[itf] = "%s_%s%s" % (base_filename, itf, ext)
      logging.getLogger().debug("Found %u network interfaces: %s" % (len(net_output_filepaths), ", ".join(net_output_filepaths)))
      with contextlib.ExitStack() as ctx:
        itf_files = {}
        for itf, itf_filepath in net_output_filepaths.items():
          itf_files[itf] = ctx.enter_context(open(itf_filepath, "wt"))
        with open(output_filepath, "rt") as output_file:
          for line in output_file:
            itf = line.split(";", 5)[3]
            if itf in itf_files:
              itf_files[itf].write(line)

    with open(output_filepath, "rt") as output_file:
      line = next(itertools.dropwhile(lambda x: not x.startswith("#"), output_file))
      columns = line[2:-1].split(";")
    dtype_columns = {SysstatDataType.LOAD: ("timestamp", "ldavg-5"),
                     SysstatDataType.CPU: ("timestamp", "%user", "%nice", "%system", "%iowait", "%steal", "%idle"),
                     SysstatDataType.MEM: ("timestamp", "kbmemused", "kbbuffers", "kbcached", "kbcommit", "kbactive", "kbdirty"),
                     SysstatDataType.SWAP: ("timestamp", "%swpused"),
                     SysstatDataType.NET: ("timestamp", "rxkB/s", "txkB/s"),
                     SysstatDataType.IO: ("timestamp", "bread/s", "bwrtn/s")}
    indexes = __class__.getColumnIndexes(dtype_columns[dtype], columns)

    return indexes, net_output_filepaths

  @staticmethod
  def getColumnIndexes(needed_column_names, column_names):
    indexes = []
    for needed_column_name in needed_column_names:
      indexes.append(column_names.index(needed_column_name) + 1)  # gnuplot indexes start at 1
    return tuple(indexes)


class Plotter:

  """ Class to plot with GNU Plot. """

  def __init__(self, report_type):
    assert(report_type in ReportType)
    self.report_type = report_type

  def plot(self, format, img_size, data_filepaths, data_indexes, data_type, reboot_times, output_filepath, smooth,
           title, data_titles, ylabel, yrange):
    assert(format in GraphFormat)

    gnuplot_code = []

    # output setup
    if format is GraphFormat.TXT:
      gnuplot_code.extend(("set terminal dumb 110,25",
                           "set output '%s'" % (output_filepath)))
    elif format is GraphFormat.PNG:
      gnuplot_code.extend(("set terminal png transparent size %u,%u font 'Liberation,9'" % tuple(img_size),
                           "set output '%s'" % (output_filepath)))
    elif format is GraphFormat.SVG:
      gnuplot_code.extend(("set terminal svg size %u,%u font 'Liberation,9'" % tuple(img_size),
                           "set output '%s'" % (output_filepath)))

    # input data setup
    if data_type is SysstatDataType.LOAD:
      gnuplot_code.append("set decimalsign locale")
    gnuplot_code.extend(("set timefmt '%s'",
                         "set datafile separator ';'"))

    # title
    gnuplot_code.append("set title '%s'" % (title))

    # caption
    gnuplot_code.append("set key outside right samplen 3 spacing 1.75")

    # x axis setup
    gnuplot_code.extend(("set xdata time",
                         "set xlabel 'Time'"))
    if self.report_type is ReportType.MONTHLY:
      gnuplot_code.append("set xtics %u" % (60 * 60 * 24 * 2))  # 2 days
    now = datetime.datetime.now()
    if self.report_type is ReportType.DAILY:
      date_to = datetime.datetime(now.year, now.month, now.day)
      date_from = date_to - datetime.timedelta(days=1)
      format_x = "%R"
    elif self.report_type is ReportType.WEEKLY:
      date_to = datetime.datetime(now.year, now.month, now.day)
      date_from = date_to - datetime.timedelta(weeks=1)
      format_x = "%a %d/%m"
    elif self.report_type is ReportType.MONTHLY:
      today = datetime.date.today()
      if today.month == 1:
        year = today.year - 1
        month = 12
      else:
        year = today.year
        month = today.month - 1
      date_from = datetime.datetime(year, month, 1)
      date_to = datetime.datetime(year, month, calendar.monthrange(year, month)[1])
      format_x = "%d"
    date_from = date_from + datetime.timedelta(seconds=time.localtime().tm_gmtoff)
    date_to = date_to + datetime.timedelta(seconds=time.localtime().tm_gmtoff)
    gnuplot_code.append("set xrange[\"%s\":\"%s\"]" % (date_from.strftime("%s"), date_to.strftime("%s")))
    gnuplot_code.append("set format x '%s'" % (format_x))

    # y axis setup
    gnuplot_code.append("set ylabel '%s'" % (ylabel))
    if yrange is not None:
      yrange = list(str(r) if r is not None else "*" for r in yrange)
      gnuplot_code.append("set yrange [%s:%s]" % (yrange[0], yrange[1]))

    # reboot lines
    for reboot_time in reboot_times:
      reboot_time = reboot_time + datetime.timedelta(seconds=time.localtime().tm_gmtoff)
      if date_from <= reboot_time <= date_to:
        gnuplot_code.append("set arrow from \"%s\",graph 0 to \"%s\",graph 1 lt 0 nohead" % (reboot_time.strftime("%s"),
                                                                                             reboot_time.strftime("%s")))

    # plot
    assert(len(data_indexes) - 1 == len(data_titles))
    plot_cmds = []
    for data_file_nickname, data_filepath in data_filepaths.items():
      for data_index, data_title in zip(data_indexes[1:], data_titles):
        if data_type is SysstatDataType.MEM:
          # convert from KB to MB
          ydata = "($%u/1000)" % (data_index)
        elif data_type is SysstatDataType.NET:
          # convert from KB/s to Mb/s
          ydata = "($%u/125)" % (data_index)
        elif data_type is SysstatDataType.IO:
          # convert from block/s to MB/s
          ydata = "($%u*512/1000000)" % (data_index)
        else:
          ydata = str(data_index)
        if data_file_nickname:
          data_title = "%s_%s" % (data_file_nickname, data_title)
        plot_cmds.append("'%s' using ($%u+%u):%s %swith lines title '%s'" % (data_filepath,
                                                                             data_indexes[0],
                                                                             time.localtime().tm_gmtoff,
                                                                             ydata,
                                                                             "smooth csplines " if smooth else "",
                                                                             data_title))
    gnuplot_code.append("plot %s" % (", ".join(plot_cmds)))

    # run gnuplot
    gnuplot_code[-1] += ";"
    gnuplot_code = ";\n".join(gnuplot_code)
    subprocess.check_output(("gnuplot",),
                            input=gnuplot_code,
                            stderr=None if logging.getLogger().isEnabledFor(logging.DEBUG) else subprocess.DEVNULL,
                            universal_newlines=True)

    # output post processing
    if format is GraphFormat.PNG and HAS_OPTIPNG:
      logging.getLogger().debug("Crunching '%s'..." % (output_filepath))
      subprocess.check_call(("optipng", "-quiet", "-o", "1", output_filepath))
    if format is GraphFormat.TXT:
      # remove first 2 bytes as they cause problems with emails
      with open(output_filepath, "rt") as output_file:
        output_file.seek(2)
        d = output_file.read()
      with open(output_filepath, "wt") as output_file:
        output_file.write(d)


if __name__ == "__main__":
  # parse args
  arg_parser = argparse.ArgumentParser(description=__doc__,
                                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  arg_parser.add_argument("report_type",
                          choices=tuple(t.name.lower() for t in ReportType),
                          help="Type of report")
  arg_parser.add_argument("mail_from",
                          help="Mail sender")
  arg_parser.add_argument("mail_to",
                          help="Mail destination")
  arg_parser.add_argument("-d",
                          "--graph-data",
                          choices=tuple(t.name.lower() for t in SysstatDataType),
                          default=tuple(t.name.lower() for t in SysstatDataType),
                          nargs="+",
                          dest="data_type",
                          help="Data to graph")
  arg_parser.add_argument("-s",
                          "--img-size",
                          type=int,
                          nargs=2,
                          default=(780, 400),
                          dest="img_size",
                          help="Graph image size")
  arg_parser.add_argument("-f",
                          "--img-format",
                          choices=tuple(t.name.lower() for t in tuple(GraphFormat)[1:]),
                          default=GraphFormat.PNG.name.lower(),
                          dest="img_format",
                          help="Image format to use (SVG breaks rendering for some email clients)")
  arg_parser.add_argument("-v",
                          "--verbosity",
                          choices=("warning", "normal", "debug"),
                          default="normal",
                          dest="verbosity",
                          help="Level of output to display")
  args = arg_parser.parse_args()
  args.data_type = tuple(SysstatDataType[dt.upper()] for dt in args.data_type)
  args.img_format = GraphFormat[args.img_format.upper()]

  # setup logger
  logging_level = {"warning": logging.WARNING,
                   "normal": logging.INFO,
                   "debug": logging.DEBUG}
  logging.basicConfig(level=logging_level[args.verbosity],
                      format="%(asctime)s %(levelname)s %(message)s")

  # display warning if optipng is missing
  if (args.img_format is GraphFormat.PNG) and (not HAS_OPTIPNG):
    logging.getLogger().warning("optipng could not be found, PNG crunching will be disabled")

  # do the job
  report_type = ReportType[args.report_type.upper()]
  with tempfile.TemporaryDirectory(prefix="%s_" % (os.path.splitext(os.path.basename(inspect.getfile(inspect.currentframe())))[0])) as temp_dir:
    sysstat_data = SysstatData(report_type, temp_dir)
    plotter = Plotter(report_type)
    plot_args = {SysstatDataType.LOAD: {"title": "Load",
                                        "data_titles": ("ldavg-5",),
                                        "ylabel": "5min load average",
                                        "yrange": (0, "%u<*" % (os.cpu_count()))},
                 SysstatDataType.CPU: {"title": "CPU",
                                       "data_titles": ("user",
                                                       "nice",
                                                       "system",
                                                       "iowait",
                                                       "steal",
                                                       "idle"),
                                       "ylabel": "CPU usage (%)",
                                       "yrange": (0, 100)},
                 SysstatDataType.MEM: {"title": "Memory",
                                       "data_titles": ("used",
                                                       "buffers",
                                                       "cached",
                                                       "commit",
                                                       "active",
                                                       "dirty"),
                                       "ylabel": "Memory used (MB)",
                                       "yrange": (0, get_total_memory_mb())},
                 SysstatDataType.SWAP: {"title": "Swap",
                                        "data_titles": ("swpused",),
                                        "ylabel": "Swap usage (%)",
                                        "yrange": (0, 100)},
                 SysstatDataType.NET: {"title": "Network",
                                       "data_titles": ("rx",
                                                       "tx"),
                                       "ylabel": "Bandwith (Mb/s)",
                                       "yrange": (0, "%u<*" % (get_max_network_speed()))},
                 SysstatDataType.IO: {"title": "IO",
                                      "data_titles": ("read",
                                                      "wrtn"),
                                      "ylabel": "Activity (MB/s)",
                                      "yrange": (0, None)}}

    graph_filepaths = {GraphFormat.TXT: [],
                       args.img_format: []}

    reboot_times = get_reboot_times()

    for data_type in args.data_type:
      # data
      logging.getLogger().info("Extracting %s data..." % (data_type.name))
      data_filepath = os.path.join(temp_dir, "%s.csv" % (data_type.name.lower()))
      indexes, data_filepaths = sysstat_data.generateData(data_type, data_filepath)
      if not data_filepaths:
        data_filepaths = {"": data_filepath}

      # plot graph
      for graph_format in (GraphFormat.TXT, args.img_format):
        logging.getLogger().info("Generating %s %s report..." % (data_type.name, graph_format.name))
        graph_filepaths[graph_format].append(os.path.join(temp_dir,
                                                          "%s.%s" % (data_type.name.lower(),
                                                                     graph_format.name.lower())))
        plotter.plot(graph_format,
                     args.img_size,
                     data_filepaths,
                     indexes,
                     data_type,
                     reboot_times,
                     graph_filepaths[graph_format][-1],
                     report_type is not ReportType.DAILY,
                     **plot_args[data_type])

    # send mail
    logging.getLogger().info("Formatting email...")
    email_data = format_email(args.mail_from,
                              args.mail_to,
                              "Sysstat %s report" % (report_type.name.lower()),
                              None,
                              args.img_format,
                              graph_filepaths[args.img_format],
                              graph_filepaths[GraphFormat.TXT])

    real_mail_from = email.utils.parseaddr(args.mail_from)[1]
    real_mail_to = email.utils.parseaddr(args.mail_to)[1]
    logging.getLogger().info("Sending email from %s to %s..." % (real_mail_from, real_mail_to))
    subprocess.check_output(("sendmail", "-f", real_mail_from, real_mail_to),
                            input=email_data,
                            universal_newlines=True)
