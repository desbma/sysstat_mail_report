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
import itertools
import logging
import lzma
import operator
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import IO, Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

try:
    # Python >= 3.8
    cmd_to_string: Callable[[Sequence[str]], str] = shlex.join
except AttributeError:
    cmd_to_string = subprocess.list2cmdline

ReportType = enum.Enum("ReportType", ("DAILY", "WEEKLY", "MONTHLY"))
SysstatDataType = enum.Enum(
    "SysstatDataType", ("LOAD", "CPU", "MEM", "SWAP", "NET", "SOCKET", "TCP4", "IO", "FS_USAGE")
)
GraphFormat = enum.Enum("GraphFormat", ("TXT", "PNG", "SVG"))

HAS_OPTIPNG = shutil.which("optipng") is not None
HAS_OXIPNG = shutil.which("oxipng") is not None


def get_total_memory_mb() -> int:
    """Return total amount of system RAM in MB."""
    with open("/proc/meminfo", "rt") as f:
        total_line = next(itertools.dropwhile(lambda x: not x.startswith("MemTotal:"), f))
    total_mem = int(tuple(filter(None, map(str.strip, total_line.split(" "))))[1]) // 1024
    logging.getLogger().info(f"Total amount of memory: {total_mem} MB")
    return total_mem


def get_max_network_speed() -> int:
    """Get maximum Ethernet network interface speed in Mb/s."""
    max_speed = 0
    interfaces = os.listdir("/sys/class/net")
    assert len(interfaces) > 1
    for interface in interfaces:
        if interface == "lo":
            continue
        filepath = f"/sys/class/net/{interface}/speed"
        try:
            with open(filepath, "rt") as f:
                new_speed = int(f.read())
        except OSError:
            logging.getLogger().warning(f"Unable to get speed of interface {interface}")
            continue
        logging.getLogger().debug(f"Speed of interface {interface}: {new_speed} Mb/s")
        max_speed = max(max_speed, new_speed)
    logging.getLogger().info(f"Maximum interface speed: {max_speed} Mb/s")
    return max_speed


def get_reboot_times() -> List[datetime.datetime]:
    """Return a list of datetime.datetime representing machine reboot times."""
    reboot_times = []
    date_regex = re.compile(r".*boot\s*([\w\s]+\d{2}:\d{2}:\d{2} \d{4}).*$")
    for i in range(1, -1, -1):
        log_filepath = "/var/log/wtmp%s" % (".%u" % (i) if i != 0 else "")
        if os.path.isfile(log_filepath):
            cmd = ("last", "-F", "-R", "reboot", "-f", log_filepath)
            logging.getLogger().debug(cmd_to_string(cmd))
            output_str = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, universal_newlines=True, check=True
            ).stdout
            output_lines = output_str.splitlines()[0:-2]
            for line in output_lines:
                date_match = date_regex.match(line)
                assert date_match is not None
                date_str = date_match.group(1).strip()
                date = datetime.datetime.strptime(date_str, r"%a %b %d %H:%M:%S %Y")
                reboot_times.append(date)
    return reboot_times


def minify_svg(svg_filepath: str) -> str:
    """Open a SVG file, and return its minified content as a string."""
    logger = logging.getLogger()
    size_before = os.path.getsize(svg_filepath)

    if shutil.which("scour"):
        method = "scour"
        cmd = (
            "scour",
            "-q",
            "--enable-id-stripping",
            "--enable-comment-stripping",
            "--shorten-ids",
            "--no-line-breaks",
            "--remove-descriptive-elements",
            svg_filepath,
        )
        logging.getLogger().debug(cmd_to_string(cmd))
        data = subprocess.run(cmd, stdin=subprocess.DEVNULL, universal_newlines=True, check=True).stdout

    else:
        method = "identity"
        with open(svg_filepath, "rt") as f:
            data = f.read()

    size_after = len(data.encode())
    if method != "identity":
        logger.debug(
            f"{method.capitalize()} SVG minification: {size_after - size_before} B "
            f"({100 * (size_after - size_before) / size_before:.2f}%)"
        )

    return data


def format_email(
    exp: str,
    dest: str,
    subject: str,
    header_text: Optional[str],
    img_format: GraphFormat,
    img_filepaths: Sequence[str],
    alternate_text_filepaths: Sequence[str],
) -> str:
    """Format a MIME email with attached images and alternate text, and return email code."""
    assert img_format in (GraphFormat.PNG, GraphFormat.SVG)

    msg = email.mime.multipart.MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = exp
    msg["To"] = dest

    # html
    html_lines = ["<html><head></head><body>"]
    if header_text is not None:
        html_lines.append(f"<pre>{header_text}</pre><br>")
    if img_format is GraphFormat.PNG:
        html_lines.append("<br>".join(f'<img src="cid:img{i}">' for i in range(len(img_filepaths))))
    elif img_format is GraphFormat.SVG:
        for img_filepath in img_filepaths:
            if img_filepath is not img_filepath[0]:
                html_lines.append("<br>")
            data = minify_svg(img_filepath)
            html_lines.append(data)
    html_lines.append("</body></html>")
    html_str = "".join(html_lines)
    html = email.mime.text.MIMEText(html_str, "html")

    # alternate text
    alternate_texts = []
    for alternate_text_filepath in alternate_text_filepaths:
        with open(alternate_text_filepath, "rt") as alternate_text_file:
            alternate_texts.append(alternate_text_file.read())
    if header_text is not None:
        text_str = f"{header_text}\n"
    else:
        text_str = ""
    text_str += "\n".join(alternate_texts)
    text = email.mime.text.MIMEText(text_str)

    msg_alt = email.mime.multipart.MIMEMultipart("alternative")
    msg_alt.attach(text)
    msg_alt.attach(html)
    msg.attach(msg_alt)

    if img_format is GraphFormat.PNG:
        for i, img_filepath in enumerate(img_filepaths):
            with open(img_filepath, "rb") as img_file:
                msg_img = email.mime.image.MIMEImage(img_file.read())
            msg_img.add_header("Content-ID", f"<img{i}>")
            msg.attach(msg_img)

    return msg.as_string()


class SysstatData:

    """Source of system stats."""

    SADF_CMDS: Dict[SysstatDataType, Tuple[Tuple[str, ...], ...]] = {
        SysstatDataType.LOAD: (("-q",),),
        SysstatDataType.CPU: (("-u",),),
        SysstatDataType.MEM: (("-r",),),
        SysstatDataType.SWAP: (("-S",),),
        SysstatDataType.NET: (("-n", "DEV"),),
        SysstatDataType.SOCKET: (("-n", "SOCK"), ("-n", "SOCK6")),
        SysstatDataType.TCP4: (("-n", "TCP"), ("-n", "ETCP")),
        SysstatDataType.IO: (("-b",),),
        SysstatDataType.FS_USAGE: (("-F", "MOUNT"),),
    }

    CSV_COLUMNS = {
        SysstatDataType.LOAD: ("timestamp", "ldavg-5"),
        SysstatDataType.CPU: ("timestamp", r"%user", "%nice", r"%system", r"%iowait", r"%steal"),
        SysstatDataType.MEM: ("timestamp", "kbmemused", "kbbuffers", "kbcached", "kbdirty"),
        SysstatDataType.SWAP: ("timestamp", r"%swpused"),
        SysstatDataType.NET: ("timestamp", "rxkB/s", "txkB/s"),
        SysstatDataType.SOCKET: ("timestamp", "tcpsck", "udpsck", "tcp6sck", "udp6sck"),
        SysstatDataType.TCP4: ("timestamp", "active/s", "passive/s", "atmptf/s"),
        SysstatDataType.IO: ("timestamp", "bread/s", "bwrtn/s"),
        SysstatDataType.FS_USAGE: ("timestamp", "%fsused"),
    }

    def __init__(self, report_type: ReportType, temp_dir: str):
        assert report_type in ReportType
        self.report_type = report_type
        self.temp_dir = temp_dir
        self.sa_filepaths: List[str] = []
        today = datetime.date.today()
        filepath_formats = [
            os.path.join("/var/log", subdir, leaf_path)
            for subdir in ("sysstat", "sa")
            for leaf_path in (r"sa%d", r"%Y%m/sa%d", r"sa%Y%m%d")
        ]

        if report_type is ReportType.DAILY:
            date = today - datetime.timedelta(days=1)
            filepath = self.getSysstatDataFilepath(date, filepath_formats, temp_dir)
            if filepath is not None:
                self.sa_filepaths.append(filepath)

        elif report_type is ReportType.WEEKLY:
            for i in range(7, 0, -1):
                date = today - datetime.timedelta(days=i)
                filepath = self.getSysstatDataFilepath(date, filepath_formats, temp_dir)
                if filepath is not None:
                    self.sa_filepaths.append(filepath)

        elif report_type is ReportType.MONTHLY:
            if today.month == 1:
                year = today.year - 1
                month = 12
            else:
                year = today.year
                month = today.month - 1
            for day in range(1, calendar.monthrange(year, month)[1] + 1):
                date = datetime.date(year, month, day)
                filepath = self.getSysstatDataFilepath(date, filepath_formats, temp_dir)
                if filepath is not None:
                    self.sa_filepaths.append(filepath)

    @staticmethod
    def decompress(in_filepath: str, out_filepath: str) -> None:
        """Decompress gzip, bzip2, or lzma input file to output file."""
        logging.getLogger().debug(f"Decompressing {in_filepath!r} to {out_filepath!r}...")
        with contextlib.ExitStack() as cm:
            ext = os.path.splitext(in_filepath)[-1].lower()
            if ext == ".gz":
                in_file: Union[gzip.GzipFile, bz2.BZ2File, lzma.LZMAFile] = cm.enter_context(
                    gzip.open(in_filepath, "rb")
                )
            elif ext == ".bz2":
                in_file = cm.enter_context(bz2.open(in_filepath, "rb"))
            elif ext == ".xz":
                in_file = cm.enter_context(lzma.open(in_filepath, "rb"))
            out_file = cm.enter_context(open(out_filepath, "wb"))
            shutil.copyfileobj(in_file, out_file)

    @classmethod
    def getSysstatDataFilepath(cls, date, filepath_formats: Sequence[str], temp_dir: str) -> Optional[str]:
        """Get data file path for requested date, decompress file in needed, return filepath or None if not found."""
        for filepath_format in filepath_formats:
            filepath = date.strftime(filepath_format)
            if not os.path.isfile(filepath):
                compressed_filepaths = (f"{filepath}.{ext}" for ext in ("gz", "bz2", "xz"))
                for compressed_filepath in compressed_filepaths:
                    if os.path.isfile(compressed_filepath):
                        filepath = os.path.join(temp_dir, os.path.basename(filepath))
                        cls.decompress(compressed_filepath, filepath)
                        return filepath
            else:
                return filepath
        logging.getLogger().warning(f"No sysstat data file for date {date}")
        return None

    def hasEnoughData(self) -> bool:
        """Return True if enough sysstat data files have been found to plot something, False instead."""
        if self.report_type is ReportType.DAILY:
            return bool(self.sa_filepaths)
        else:
            return len(self.sa_filepaths) >= 2

    def generateRawCsv(self, dtype: SysstatDataType, sa_filepath: str, output_file: IO[str]) -> None:
        """Extract stats from sa file and write them in CSV format to text file."""
        tmp_csv_files = []
        with contextlib.ExitStack() as cm:
            for i, sadf_cmd in enumerate(self.SADF_CMDS[dtype]):
                tmp_csv_file = cm.enter_context(
                    tempfile.TemporaryFile(
                        "w+t", prefix=f"{dtype.name.lower()}_{i:02}", suffix=".csv", dir=self.temp_dir
                    )
                )
                cmd = ["sadf", "-d", "-U", "--"]
                cmd.extend(sadf_cmd)
                cmd.append(sa_filepath)
                logging.getLogger().debug(cmd_to_string(cmd))
                subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=tmp_csv_file, universal_newlines=True, check=True)
                tmp_csv_file.seek(0)
                tmp_csv_files.append(tmp_csv_file)
            self.mergeCsvFiles(tmp_csv_files, output_file)

    def mergeCsvFiles(self, source_files: Sequence[IO[str]], dest_file: IO[str]) -> None:
        """Merge several CSV files into one with same number of lines."""
        filtered_source_files = []
        with contextlib.ExitStack() as cm:
            sources_columns = []
            for source_file in source_files:
                # get columns
                sources_columns.append(self.getCsvColumns(source_file))

                # filter input files
                filtered_source_file = cm.enter_context(tempfile.TemporaryFile("w+t", suffix=".csv", dir=self.temp_dir))
                self.filterRawCsv(source_file, filtered_source_file)
                filtered_source_file.seek(0)
                filtered_source_files.append(filtered_source_file)

            # merge line per line
            first_line = True
            for sources_line in zip(*filtered_source_files):
                added_fields_names = []
                row = []
                for source_columns, source_line in zip(sources_columns, sources_line):
                    fields = source_line.rstrip().split(";")
                    for field_name, field in zip(source_columns, fields):
                        if field_name not in added_fields_names:
                            added_fields_names.append(field_name)
                            row.append(field)
                if first_line:
                    # write column names
                    dest_file.write(f"# {';'.join(added_fields_names)}\n")
                    first_line = False
                dest_file.write(f"{';'.join(row)}\n")

    def getCsvColumns(self, csv_file: IO[str]) -> Sequence[str]:
        """Extract column names from CSV file."""
        line = next(itertools.dropwhile(lambda x: not x.startswith("#"), csv_file))
        columns = line[2:-1].split(";")
        return columns

    def filterRawCsv(self, in_file: IO[str], out_file: IO[str]) -> None:
        """Filter CSV file by removing lines that gnuplot would not parse."""
        for line in in_file:
            if line.startswith("#"):
                # comment lines are correctly ignored by gnuplot, but we remove them for clarity
                # (they can appear several times and in the middle of the CSV file if a reboot occurs)
                continue
            fields = line.rstrip().split(";")
            if int(fields[1]) == -1:  # fields[3] == "LINUX-RESTART"
                continue
            out_file.write(line)

    def generateDataToPlot(self, dtype: SysstatDataType, output_filepath: str) -> Tuple[Sequence[int], Dict[str, str]]:
        """
        Generate data to plot (';' separated values).

        Return indexes of columns to use in output, and a dictionary of name -> filepath output datafiles if the
        provided output file had to be split.
        """
        assert dtype in SysstatDataType
        output_filepaths = {}

        with open(output_filepath, "w+t") as output_file:
            for sa_filepath in self.sa_filepaths:
                self.generateRawCsv(dtype, sa_filepath, output_file)

            # get columns
            output_file.seek(0)
            columns = self.getCsvColumns(output_file)

            if dtype in (SysstatDataType.NET, SysstatDataType.FS_USAGE):
                # find varying data field in csv file
                data_field_info = {
                    SysstatDataType.NET: ("network interfaces", 3),
                    SysstatDataType.FS_USAGE: ("filesystems", 3),
                }
                data_field_name, data_field_index = data_field_info[dtype]
                data_fields = list(self.getUniqueFieldValuesFromCsv(output_file, data_field_index))
                data_fields.sort()
                logging.getLogger().debug(f"Found {len(data_fields)} {data_field_name}: {', '.join(data_fields)}")
                base_filename, ext = os.path.splitext(output_filepath)
                for i, df in enumerate(data_fields, 1):
                    output_filepaths[df] = f"{base_filename}_{i}{ext}"

                # split file by varying field
                output_file.seek(0)
                self.splitCsvFile(output_file, 3, output_filepaths)

        indexes = self.getColumnIndexes(self.CSV_COLUMNS[dtype], columns)

        return indexes, output_filepaths

    @staticmethod
    def getColumnIndexes(needed_column_names: Sequence[str], column_names: Sequence[str]) -> Sequence[int]:
        """Return column indexes matching the given column names, to be used by Gnuplot."""
        indexes = []
        for needed_column_name in needed_column_names:
            indexes.append(column_names.index(needed_column_name) + 1)  # gnuplot indexes start at 1
        return tuple(indexes)

    @staticmethod
    def splitCsvFile(input_file: IO[str], column_index: int, output_files: Dict[str, str]) -> None:
        """Split input file according to a given column index, and output filepaths dict."""
        with contextlib.ExitStack() as ctx:
            files = {}
            for k, filepath in output_files.items():
                files[k] = ctx.enter_context(open(filepath, "wt"))
            for line in input_file:
                if line.startswith("#"):
                    continue
                fields = line.split(";")
                k = fields[column_index]
                files[k].write(line)

    @staticmethod
    def getUniqueFieldValuesFromCsv(in_file: IO[str], index: int) -> Set[str]:
        """Extract unique field values from a CSV file."""
        values = set()
        for line in itertools.filterfalse(operator.methodcaller("startswith", "#"), in_file):
            fields = line.split(";", index + 2)
            value = fields[index]
            values.add(value)
        return values


class Plotter:

    """Class to plot with GNU Plot."""

    PLOT_ARGS: Dict[SysstatDataType, Dict[str, Any]] = {
        SysstatDataType.LOAD: {
            "title": "Load",
            "data_titles": ("ldavg-5",),
            "ylabel": "5min load average",
            "yrange": (0, f"{os.cpu_count()}<*"),
        },
        SysstatDataType.CPU: {
            "title": "CPU",
            "data_titles": ("user", "nice", "system", "iowait", "steal"),
            "ylabel": "CPU usage (%)",
            "yrange": (0, 100),
        },
        SysstatDataType.MEM: {
            "title": "Memory",
            "data_titles": ("other", "buffers", "cached", "dirty"),
            "ylabel": "Memory used (MB)",
            "yrange": (0, get_total_memory_mb()),
        },
        SysstatDataType.SWAP: {
            "title": "Swap",
            "data_titles": ("swpused",),
            "ylabel": "Swap usage (%)",
            "yrange": (0, 100),
        },
        SysstatDataType.NET: {
            "title": "Network",
            "data_titles": ("rx", "tx"),
            "ylabel": "Bandwith (Mb/s)",
            "yrange": (0, f"{get_max_network_speed()}<*"),
        },
        SysstatDataType.SOCKET: {
            "title": "Sockets",
            "data_titles": ("tcp4", "udp4", "tcp6", "udp6"),
            "ylabel": "Socket count",
            "yrange": (0, None),
        },
        SysstatDataType.TCP4: {
            "title": "TCP/IPv4 sockets",
            "data_titles": ("active", "passive", "atmptf"),
            "ylabel": "Transitions (socket/s)",
            "yrange": (0, None),
        },
        SysstatDataType.IO: {
            "title": "IO",
            "data_titles": ("read", "wrtn"),
            "ylabel": "Activity (MB/s)",
            "yrange": (0, None),
        },
        SysstatDataType.FS_USAGE: {
            "title": "Filesystem usage",
            "data_titles": ("",),
            "ylabel": "Usage (%)",
            "yrange": (0, 100),
        },
    }

    def __init__(self, report_type: ReportType):
        assert report_type in ReportType
        self.report_type = report_type

    def plot(  # noqa: C901
        self,
        format: GraphFormat,
        img_size: Tuple[int, int],
        data_filepaths: Dict[str, str],
        data_indexes: Sequence[int],
        data_type: SysstatDataType,
        reboot_times: Sequence[datetime.datetime],
        output_filepath: str,
        smooth: bool,
        title: str,
        data_titles: Sequence[str],
        ylabel: str,
        yrange: Tuple[Optional[int], Optional[int]],
    ) -> None:
        """Plot graph using Gnuplot."""
        assert format in GraphFormat

        gnuplot_code_lines: List[str] = []

        # output setup
        if format is GraphFormat.TXT:
            gnuplot_code_lines.extend(("set terminal dumb 110,25", f"set output '{output_filepath}'"))
        elif format is GraphFormat.PNG:
            gnuplot_code_lines.extend(
                (
                    f"set terminal png transparent size {img_size[0]},{img_size[1]} font 'Liberation,9' noenhanced",
                    f"set output '{output_filepath}'",
                )
            )
        elif format is GraphFormat.SVG:
            gnuplot_code_lines.extend(
                (
                    f"set terminal svg size {img_size[0]},{img_size[1]} font 'Liberation,9' noenhanced",
                    f"set output '{output_filepath}'",
                )
            )

        # input data setup
        if data_type is SysstatDataType.LOAD:
            gnuplot_code_lines.append("set decimalsign locale")
        gnuplot_code_lines.extend((r"set timefmt '%s'", "set datafile separator ';'"))

        # title
        gnuplot_code_lines.append(f"set title '{title}'")

        # caption
        gnuplot_code_lines.append("set key outside right samplen 3 spacing 1.75 width 2")

        # x axis setup
        gnuplot_code_lines.extend(("set xdata time", "set xlabel 'Time'"))
        if self.report_type is ReportType.MONTHLY:
            gnuplot_code_lines.append(f"set xtics {60 * 60 * 24 * 2}")  # 2 days
        now = datetime.datetime.now()
        if self.report_type is ReportType.DAILY:
            date_to = datetime.datetime(now.year, now.month, now.day)
            date_from = date_to - datetime.timedelta(days=1)
            format_x = "%R"
        elif self.report_type is ReportType.WEEKLY:
            date_to = datetime.datetime(now.year, now.month, now.day)
            date_from = date_to - datetime.timedelta(weeks=1)
            format_x = r"%a %d/%m"
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
            format_x = r"%d"
        date_from = date_from + datetime.timedelta(seconds=time.localtime().tm_gmtoff)
        date_to = date_to + datetime.timedelta(seconds=time.localtime().tm_gmtoff)
        gnuplot_code_lines.append('set xrange["%s":"%s"]' % (date_from.strftime(r"%s"), date_to.strftime(r"%s")))
        gnuplot_code_lines.append(f"set format x '{format_x}'")

        # y axis setup
        gnuplot_code_lines.append(f"set ylabel '{ylabel}'")
        if yrange is not None:
            yrange_str = tuple(str(r) if r is not None else "*" for r in yrange)
            gnuplot_code_lines.append(f"set yrange [{yrange_str[0]}:{yrange_str[1]}]")

        # reboot lines
        for reboot_time in reboot_times:
            reboot_time = reboot_time + datetime.timedelta(seconds=time.localtime().tm_gmtoff)
            if date_from <= reboot_time <= date_to:
                reboot_ts = reboot_time.strftime(r"%s")
                gnuplot_code_lines.append(
                    f'set arrow from "{reboot_ts}",graph 0 to "{reboot_ts}",graph 1 lt 0 nohead front'
                )
                gnuplot_code_lines.append(
                    f'set label "reboot" at "{reboot_ts}",graph 0 right rotate by 45 font \'Liberation,7\''
                )

        # plot
        assert len(data_indexes) - 1 == len(data_titles)
        plot_cmds = []
        stacked = data_type in (SysstatDataType.CPU, SysstatDataType.MEM)
        for data_file_nickname, data_filepath in data_filepaths.items():
            prev_ydata = None
            for data_index, data_title in zip(data_indexes[1:], data_titles):
                if data_type is SysstatDataType.MEM:
                    ydata = f"${data_index}"
                    if data_title == "other":
                        # substract other memory columns except free
                        data_indexes_to_sub = []
                        for data_index_to_sub, data_title_to_sub in zip(data_indexes[1:], data_titles):
                            if data_title_to_sub in ("other", "free"):
                                continue
                            data_indexes_to_sub.append(data_index_to_sub)
                        ydata = "(%s-%s)" % (ydata, "-".join(f"${i}" for i in data_indexes_to_sub))
                    # convert from KB to MB
                    ydata = f"({ydata}/1000)"
                elif data_type is SysstatDataType.NET:
                    # convert from KB/s to Mb/s
                    ydata = f"(${data_index}/125)"
                elif data_type is SysstatDataType.IO:
                    # convert from block/s to MB/s
                    ydata = f"(${data_index}*512/1000000)"
                else:
                    ydata = f"(${data_index})"
                if data_file_nickname:
                    if not data_title:
                        data_title = data_file_nickname
                    else:
                        data_title = f"{data_file_nickname}_{data_title}"
                if stacked:
                    plot_type = "filledcurve x1"
                    if prev_ydata is not None:
                        # values are cumulative
                        ydata = f"({ydata}+{prev_ydata})"
                else:
                    plot_type = "line"
                plot_cmds.append(
                    f"'{data_filepath}' using (${data_indexes[0]}+{time.localtime().tm_gmtoff}):{ydata}"
                    f" {'smooth bezier ' if smooth else ''}with {plot_type} title '{data_title}'"
                )
                prev_ydata = ydata
        if stacked:
            plot_cmds.reverse()
        gnuplot_code_lines.append(f"plot {', '.join(plot_cmds)}")

        # run gnuplot
        gnuplot_code_lines[-1] += ";"
        gnuplot_code = ";\n".join(gnuplot_code_lines)
        logging.getLogger().debug(gnuplot_code)
        subprocess.run(
            ("gnuplot",),
            input=gnuplot_code,
            stderr=None if logging.getLogger().isEnabledFor(logging.DEBUG) else subprocess.DEVNULL,
            universal_newlines=True,
            check=True,
        )

        # output post processing
        if format is GraphFormat.PNG and (HAS_OPTIPNG or HAS_OXIPNG):
            logging.getLogger().debug(f"Crunching {output_filepath!r}...")
            if HAS_OXIPNG:
                cmd: Sequence[str] = ("oxipng", "-q", "-s", output_filepath)
            else:
                cmd = ("optipng", "-quiet", "-o", "1", output_filepath)
            logging.getLogger().debug(cmd_to_string(cmd))
            subprocess.run(cmd, check=True)
        if format is GraphFormat.TXT:
            # remove first 2 bytes as they cause problems with emails
            with open(output_filepath, "rt") as output_file:
                output_file.seek(2)
                d = output_file.read()
            with open(output_filepath, "wt") as output_file:
                output_file.write(d)


if __name__ == "__main__":
    # parse args
    arg_parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    arg_parser.add_argument("report_type", choices=tuple(t.name.lower() for t in ReportType), help="Type of report")
    arg_parser.add_argument("mail_from", help="Mail sender")
    arg_parser.add_argument("mail_to", help="Mail destination")
    arg_parser.add_argument(
        "-d",
        "--graph-data",
        choices=tuple(t.name.lower() for t in SysstatDataType),
        default=tuple(
            t.name.lower()
            for t in SysstatDataType
            if t
            not in (
                SysstatDataType.LOAD,
                SysstatDataType.SWAP,
                SysstatDataType.SOCKET,
                SysstatDataType.TCP4,
                SysstatDataType.FS_USAGE,
            )
        ),
        nargs="+",
        dest="data_type",
        help="Data to graph",
    )
    arg_parser.add_argument(
        "-s", "--img-size", type=int, nargs=2, default=(780, 400), dest="img_size", help="Graph image size"
    )
    arg_parser.add_argument(
        "-f",
        "--img-format",
        choices=tuple(t.name.lower() for t in tuple(GraphFormat)[1:]),
        default=GraphFormat.PNG.name.lower(),
        dest="img_format",
        help="Image format to use (SVG breaks rendering for some email clients)",
    )
    arg_parser.add_argument(
        "-v",
        "--verbosity",
        choices=("warning", "normal", "debug"),
        default="normal",
        dest="verbosity",
        help="Level of output to display",
    )
    args = arg_parser.parse_args()
    args.data_type = tuple(SysstatDataType[dt.upper()] for dt in args.data_type)
    args.img_format = GraphFormat[args.img_format.upper()]

    # setup logger
    logging_level = {"warning": logging.WARNING, "normal": logging.INFO, "debug": logging.DEBUG}
    logging.basicConfig(level=logging_level[args.verbosity], format=r"%(asctime)s %(levelname)s %(message)s")

    # display warning if optipng is missing
    if (args.img_format is GraphFormat.PNG) and (not HAS_OPTIPNG):
        logging.getLogger().warning("optipng could not be found, PNG crunching will be disabled")

    # do the job
    report_type = ReportType[args.report_type.upper()]
    with tempfile.TemporaryDirectory(
        prefix=f"{os.path.splitext(os.path.basename(inspect.getfile(inspect.currentframe())))[0]}_"  # type: ignore
    ) as temp_dir:
        sysstat_data = SysstatData(report_type, temp_dir)
        if not sysstat_data.hasEnoughData():
            logging.getLogger().error("Not enough data files")
            exit(1)

        plotter = Plotter(report_type)
        graph_filepaths: Dict[GraphFormat, List[str]] = {GraphFormat.TXT: [], args.img_format: []}
        reboot_times = get_reboot_times()

        for data_type in args.data_type:
            # data
            logging.getLogger().info(f"Extracting {data_type.name} data...")
            data_filepath = os.path.join(temp_dir, f"{data_type.name.lower()}.csv")
            indexes, data_filepaths = sysstat_data.generateDataToPlot(data_type, data_filepath)
            if not data_filepaths:
                data_filepaths = {"": data_filepath}

            # plot graph
            for graph_format in (GraphFormat.TXT, args.img_format):
                logging.getLogger().info(f"Generating {data_type.name} {graph_format.name} report...")
                graph_filepaths[graph_format].append(
                    os.path.join(temp_dir, f"{data_type.name.lower()}.{graph_format.name.lower()}")
                )
                plotter.plot(
                    graph_format,
                    args.img_size,
                    data_filepaths,
                    indexes,
                    data_type,
                    reboot_times,
                    graph_filepaths[graph_format][-1],
                    report_type is not ReportType.DAILY,
                    **Plotter.PLOT_ARGS[data_type],
                )

        # send mail
        logging.getLogger().info("Formatting email...")
        email_data = format_email(
            args.mail_from,
            args.mail_to,
            f"Sysstat {report_type.name.lower()} report",
            None,
            args.img_format,
            graph_filepaths[args.img_format],
            graph_filepaths[GraphFormat.TXT],
        )

        real_mail_from = email.utils.parseaddr(args.mail_from)[1]
        real_mail_to = email.utils.parseaddr(args.mail_to)[1]
        logging.getLogger().info(f"Sending email from {real_mail_from!r} to {real_mail_to!r}...")
        cmd = ("sendmail", "-f", real_mail_from, real_mail_to)
        logging.getLogger().debug(cmd_to_string(cmd))
        subprocess.run(cmd, input=email_data, universal_newlines=True, check=True)
