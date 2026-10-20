#!/usr/bin/env python3

"""Generate and send a sysstat mail report."""

import argparse
import bz2
import contextlib
import datetime
import email.message
import email.utils
import enum
import gzip
import logging
import lzma
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, ClassVar, NamedTuple

ReportType = enum.StrEnum("ReportType", ("DAILY", "WEEKLY", "MONTHLY"))
SysstatDataType = enum.StrEnum(
    "SysstatDataType",
    ("LOAD", "CPU", "MEM", "SWAP", "NET", "SOCKET", "TCP4", "IO", "FS_USAGE"),
)
GraphFormat = enum.StrEnum("GraphFormat", ("TXT", "PNG", "SVG"))

HAS_OPTIPNG = shutil.which("optipng") is not None
HAS_OXIPNG = shutil.which("oxipng") is not None

DECOMPRESSORS = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}

logger = logging.getLogger(__name__)


def get_total_memory_mib() -> int:
    """Return total amount of system RAM in MiB."""
    with Path("/proc/meminfo").open() as f:
        total_line = next(line for line in f if line.startswith("MemTotal:"))
    total_mem = int(total_line.split()[1]) // 1024
    logger.info(f"Total amount of memory: {total_mem} MiB")
    return total_mem


def get_max_network_speed() -> int:
    """Get maximum Ethernet network interface speed in Mb/s."""
    max_speed = 0
    interface_dirs = tuple(Path("/sys/class/net").iterdir())
    assert len(interface_dirs) > 1
    for interface_dir in interface_dirs:
        interface = interface_dir.name
        if interface == "lo":
            continue
        try:
            new_speed = int((interface_dir / "speed").read_text())
        except OSError:
            logger.warning(f"Unable to get speed of interface {interface}")
            continue
        logger.debug(f"Speed of interface {interface}: {new_speed} Mb/s")
        max_speed = max(max_speed, new_speed)
    logger.info(f"Maximum interface speed: {max_speed} Mb/s")
    return max_speed


def minify_svg(svg_filepath: Path) -> str:
    """Open a SVG file, and return its minified content as a string."""
    if not shutil.which("scour"):
        return svg_filepath.read_text()

    size_before = svg_filepath.stat().st_size
    cmd = (
        "scour",
        "-q",
        "--enable-id-stripping",
        "--enable-comment-stripping",
        "--shorten-ids",
        "--no-line-breaks",
        "--remove-descriptive-elements",
        str(svg_filepath),
    )
    logger.debug(shlex.join(cmd))
    data = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        check=True,
    ).stdout
    size_delta = len(data.encode()) - size_before
    logger.debug(
        f"Scour SVG minification: {size_delta} B "
        f"({100 * size_delta / size_before:.2f}%)"
    )
    return data


def format_email(
    mail_from: str,
    mail_to: str,
    subject: str,
    img_format: GraphFormat,
    img_filepaths: Sequence[Path],
    alternate_text_filepaths: Sequence[Path],
) -> str:
    """Format a MIME email with attached images and alternate text, and return email code."""
    assert img_format in (GraphFormat.PNG, GraphFormat.SVG)

    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(
        "\n".join(filepath.read_text() for filepath in alternate_text_filepaths)
    )

    if img_format is GraphFormat.PNG:
        html_body = "<br>".join(
            f'<img src="cid:img{i}">' for i in range(len(img_filepaths))
        )
    else:
        html_body = "<br>".join(map(minify_svg, img_filepaths))
    msg.add_alternative(
        f"<html><head></head><body>{html_body}</body></html>", subtype="html"
    )

    if img_format is GraphFormat.PNG:
        html_part = msg.get_body(("html",))
        assert html_part is not None
        for i, img_filepath in enumerate(img_filepaths):
            html_part.add_related(
                img_filepath.read_bytes(), "image", "png", cid=f"<img{i}>"
            )

    return msg.as_string()


def report_period(report_type: ReportType) -> tuple[datetime.date, datetime.date]:
    """Return the first day of the report period and the day after its last."""
    today = datetime.date.today()
    if report_type is ReportType.DAILY:
        return today - datetime.timedelta(days=1), today
    if report_type is ReportType.WEEKLY:
        return today - datetime.timedelta(weeks=1), today
    month_start = today.replace(day=1)
    return (month_start - datetime.timedelta(days=1)).replace(day=1), month_start


class SysstatData:
    """Source of system stats."""

    SADF_ARGS: ClassVar[dict[SysstatDataType, tuple[str, ...]]] = {
        SysstatDataType.LOAD: ("-q",),
        SysstatDataType.CPU: ("-u",),
        SysstatDataType.MEM: ("-r",),
        SysstatDataType.SWAP: ("-S",),
        SysstatDataType.NET: ("-n", "DEV"),
        SysstatDataType.SOCKET: ("-n", "SOCK,SOCK6"),
        SysstatDataType.TCP4: ("-n", "TCP,ETCP"),
        SysstatDataType.IO: ("-b",),
        SysstatDataType.FS_USAGE: ("-F", "MOUNT"),
    }

    CSV_COLUMNS: ClassVar[dict[SysstatDataType, tuple[str, ...]]] = {
        SysstatDataType.LOAD: ("timestamp", "ldavg-5"),
        SysstatDataType.CPU: (
            "timestamp",
            r"%user",
            "%nice",
            r"%system",
            r"%iowait",
            r"%steal",
        ),
        SysstatDataType.MEM: (
            "timestamp",
            "kbmemused",
            "kbbuffers",
            "kbcached",
            "kbdirty",
        ),
        SysstatDataType.SWAP: ("timestamp", r"%swpused"),
        SysstatDataType.NET: ("timestamp", "rxkB/s", "txkB/s"),
        SysstatDataType.SOCKET: ("timestamp", "tcpsck", "udpsck", "tcp6sck", "udp6sck"),
        SysstatDataType.TCP4: ("timestamp", "active/s", "passive/s", "atmptf/s"),
        SysstatDataType.IO: ("timestamp", "bread/s", "bwrtn/s"),
        SysstatDataType.FS_USAGE: ("timestamp", "%fsused"),
    }

    def __init__(self, report_type: ReportType, temp_dir: Path):
        self.temp_dir = temp_dir
        self.sa_filepaths: list[Path] = []
        filepath_formats = [
            f"/var/log/{subdir}/{leaf_path}"
            for subdir in ("sysstat", "sa")
            for leaf_path in (r"sa%Y%m%d", r"%Y%m/sa%d", r"sa%d")
        ]
        date_from, date_to = report_period(report_type)
        for offset in range((date_to - date_from).days):
            date = date_from + datetime.timedelta(days=offset)
            filepath = self.get_sysstat_data_filepath(date, filepath_formats, temp_dir)
            if filepath is not None:
                self.sa_filepaths.append(filepath)

    @staticmethod
    def get_sysstat_data_filepath(
        date: datetime.date, filepath_formats: Sequence[str], temp_dir: Path
    ) -> Path | None:
        """Get data file path for requested date, decompress file in needed, return filepath or None if not found."""
        for filepath_format in filepath_formats:
            filepath = Path(date.strftime(filepath_format))
            if filepath.is_file():
                return filepath
            for ext, opener in DECOMPRESSORS.items():
                compressed_filepath = Path(f"{filepath}{ext}")
                if not compressed_filepath.is_file():
                    continue
                decompressed_filepath = temp_dir / filepath.name
                logger.debug(
                    f"Decompressing {str(compressed_filepath)!r} to {str(decompressed_filepath)!r}..."
                )
                with (
                    opener(compressed_filepath, "rb") as in_file,
                    decompressed_filepath.open("wb") as out_file,
                ):
                    shutil.copyfileobj(in_file, out_file)
                return decompressed_filepath
        logger.warning(f"No sysstat data file for date {date}")
        return None

    @staticmethod
    def run_sadf(sa_filepath: Path, *args: str) -> str:
        """Return CSV data extracted from a sysstat data file."""
        cmd = ("sadf", "-d", "-U", *args, str(sa_filepath))
        logger.debug(shlex.join(cmd))
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            check=True,
            # decimal point, whatever the locale
            env=dict(os.environ, LC_ALL="C"),
        ).stdout

    def get_reboot_times(self) -> list[int]:
        """Return machine reboot times, as Unix timestamps."""
        reboot_times = []
        for sa_filepath in self.sa_filepaths:
            lines = self.run_sadf(sa_filepath).splitlines()
            reboot_times.extend(
                int(line.split(";", 3)[2]) for line in lines if ";LINUX-RESTART" in line
            )
        return reboot_times

    def generate_raw_csv(
        self, data_type: SysstatDataType, sa_filepath: Path, output_file: IO[str]
    ) -> None:
        """Extract stats from sa file and write them in CSV format to text file."""
        # one line per sample for several activities
        horizontal = (
            ("-h",)
            if data_type in (SysstatDataType.SOCKET, SysstatDataType.TCP4)
            else ()
        )
        lines = self.run_sadf(
            sa_filepath, *horizontal, "--", *self.SADF_ARGS[data_type]
        ).splitlines(keepends=True)
        # a file holding only reboot records has no header
        header = next((line for line in lines if line.startswith("#")), None)
        if header is None:
            logger.warning(
                f"No {data_type.name} samples in {str(sa_filepath)!r}, skipping it"
            )
            return
        if output_file.tell() == 0:
            output_file.write(header)
        # skip header lines, repeated after each reboot, and LINUX-RESTART records
        output_file.writelines(
            line
            for line in lines
            if not line.startswith("#") and ";LINUX-RESTART" not in line
        )

    def generate_data_to_plot(
        self, data_type: SysstatDataType
    ) -> tuple[Sequence[int], dict[str, Path]]:
        """Write plot data to CSV files, and return their gnuplot column indexes and their paths, keyed by the data field value they hold, if any."""
        output_filepath = self.temp_dir / f"{data_type}.csv"
        output_filepaths = {}

        with output_filepath.open("w+") as output_file:
            for sa_filepath in self.sa_filepaths:
                self.generate_raw_csv(data_type, sa_filepath, output_file)
            if output_file.tell() == 0:
                raise RuntimeError(f"No {data_type.name} samples in the report period")

            # get columns
            output_file.seek(0)
            columns = output_file.readline()[2:-1].split(";")

            if data_type in (SysstatDataType.NET, SysstatDataType.FS_USAGE):
                # find varying data field in csv file
                data_field_index = 3
                data_fields = sorted(
                    {
                        line.split(";", data_field_index + 2)[data_field_index]
                        for line in output_file
                        if not line.startswith("#")
                    }
                )
                logger.debug(
                    f"Found {len(data_fields)} {data_type} values: {', '.join(data_fields)}"
                )
                for i, data_field in enumerate(data_fields, 1):
                    output_filepaths[data_field] = output_filepath.with_stem(
                        f"{output_filepath.stem}_{i}"
                    )

                # split file by varying field
                output_file.seek(0)
                self.split_csv_file(output_file, data_field_index, output_filepaths)

        # gnuplot indexes start at 1
        indexes = tuple(columns.index(name) + 1 for name in self.CSV_COLUMNS[data_type])

        return indexes, output_filepaths or {"": output_filepath}

    @staticmethod
    def split_csv_file(
        input_file: IO[str], column_index: int, output_filepaths: dict[str, Path]
    ) -> None:
        """Split input file according to a given column index, and output filepaths dict."""
        with contextlib.ExitStack() as ctx:
            output_streams = {}
            for data_field, filepath in output_filepaths.items():
                output_streams[data_field] = ctx.enter_context(filepath.open("w"))
            for line in input_file:
                if line.startswith("#"):
                    continue
                fields = line.split(";")
                data_field = fields[column_index]
                output_streams[data_field].write(line)


class PlotConfig(NamedTuple):
    """Gnuplot labels and y axis range of a data type."""

    title: str
    data_titles: tuple[str, ...]
    ylabel: str
    yrange: tuple[int, int | str | None]


PLOT_CONFIGS = {
    SysstatDataType.LOAD: PlotConfig(
        "Load", ("ldavg-5",), "5min load average", (0, f"{os.cpu_count()}<*")
    ),
    SysstatDataType.CPU: PlotConfig(
        "CPU",
        ("user", "nice", "system", "iowait", "steal"),
        "CPU usage (%)",
        (0, 100),
    ),
    SysstatDataType.MEM: PlotConfig(
        "Memory",
        ("used", "buffers", "cached", "dirty"),
        "Memory used (MiB)",
        (0, get_total_memory_mib()),
    ),
    SysstatDataType.SWAP: PlotConfig("Swap", ("swpused",), "Swap usage (%)", (0, 100)),
    SysstatDataType.NET: PlotConfig(
        "Network", ("rx", "tx"), "Bandwith (Mb/s)", (0, f"{get_max_network_speed()}<*")
    ),
    SysstatDataType.SOCKET: PlotConfig(
        "Sockets", ("tcp4", "udp4", "tcp6", "udp6"), "Socket count", (0, None)
    ),
    SysstatDataType.TCP4: PlotConfig(
        "TCP/IPv4 sockets",
        ("active", "passive", "atmptf"),
        "Transitions (socket/s)",
        (0, None),
    ),
    SysstatDataType.IO: PlotConfig(
        "IO", ("read", "wrtn"), "Activity (MB/s)", (0, None)
    ),
    SysstatDataType.FS_USAGE: PlotConfig(
        "Filesystem usage", ("",), "Usage (%)", (0, 100)
    ),
}


def plot(
    report_type: ReportType,
    graph_format: GraphFormat,
    img_size: tuple[int, int],
    data_filepaths: dict[str, Path],
    data_indexes: Sequence[int],
    data_type: SysstatDataType,
    reboot_times: Sequence[int],
    output_filepath: Path,
) -> None:
    """Plot graph using Gnuplot."""
    title, data_titles, ylabel, yrange = PLOT_CONFIGS[data_type]

    # output setup
    if graph_format is GraphFormat.TXT:
        terminal = "dumb 110,25"
    else:
        terminal = (
            f"{graph_format} size {img_size[0]},{img_size[1]} "
            "font 'Liberation,9' noenhanced"
        )
    gnuplot_code_lines = [
        f"set terminal {terminal}",
        f"set output '{output_filepath}'",
        r"set timefmt '%s'",
        "set datafile separator ';'",
        f"set title '{title}'",
        "set key outside right samplen 3 spacing 1.75 width 2",
        "set xdata time",
        "set xlabel 'Time'",
    ]

    # x axis setup
    if report_type is ReportType.MONTHLY:
        gnuplot_code_lines.append(f"set xtics {60 * 60 * 24 * 2}")  # 2 days
    gmtoff = time.localtime().tm_gmtoff
    ts_from, ts_to = (
        int(datetime.datetime.combine(date, datetime.time()).timestamp()) + gmtoff
        for date in report_period(report_type)
    )
    format_x = {
        ReportType.DAILY: "%R",
        ReportType.WEEKLY: r"%a %d/%m",
        ReportType.MONTHLY: r"%d",
    }[report_type]
    gnuplot_code_lines.append(f'set xrange["{ts_from}":"{ts_to}"]')
    gnuplot_code_lines.append(f"set format x '{format_x}'")

    # y axis setup
    gnuplot_code_lines.append(f"set ylabel '{ylabel}'")
    yrange_str = ":".join("*" if value is None else str(value) for value in yrange)
    gnuplot_code_lines.append(f"set yrange [{yrange_str}]")

    # reboot lines
    for reboot_time in reboot_times:
        reboot_ts = reboot_time + gmtoff
        if ts_from <= reboot_ts <= ts_to:
            gnuplot_code_lines += [
                f'set arrow from "{reboot_ts}",graph 0 to "{reboot_ts}",graph 1 lt 0 nohead front',
                f'set label "reboot" at "{reboot_ts}",graph 0 right rotate by 45 font \'Liberation,7\'',
            ]

    # plot
    plot_cmds = []
    stacked = data_type is SysstatDataType.CPU
    plot_type = "filledcurve x1" if stacked else "line"
    smooth = report_type is not ReportType.DAILY
    unit_suffix = {
        SysstatDataType.MEM: "/1024",  # KiB to MiB
        SysstatDataType.NET: "*8192/1000000",  # KiB/s to Mb/s
        SysstatDataType.IO: "*512/1000000",  # block/s to MB/s
    }.get(data_type, "")
    for data_file_nickname, data_filepath in data_filepaths.items():
        prev_ydata = None
        for data_index, data_title in zip(data_indexes[1:], data_titles, strict=True):
            ydata = f"(${data_index}{unit_suffix})"
            data_title = "_".join(filter(None, (data_file_nickname, data_title)))
            data_title = data_title.replace("'", "''")
            if stacked and prev_ydata is not None:
                # values are cumulative
                ydata = f"({ydata}+{prev_ydata})"
            plot_cmds.append(
                f"'{data_filepath}' using (${data_indexes[0]}+{gmtoff}):{ydata}"
                f" {'smooth bezier ' if smooth else ''}with {plot_type} title '{data_title}'"
            )
            prev_ydata = ydata
    if stacked:
        plot_cmds.reverse()
    gnuplot_code_lines.append(f"plot {', '.join(plot_cmds)}")

    # run gnuplot
    gnuplot_code = "".join(f"{line};\n" for line in gnuplot_code_lines)
    logger.debug(gnuplot_code)
    subprocess.run(
        ("gnuplot",),
        input=gnuplot_code,
        stdout=None,
        stderr=None if logger.isEnabledFor(logging.DEBUG) else subprocess.DEVNULL,
        text=True,
        check=True,
    )

    # output post processing
    if graph_format is GraphFormat.PNG and (HAS_OPTIPNG or HAS_OXIPNG):
        logger.debug(f"Crunching {str(output_filepath)!r}...")
        if HAS_OXIPNG:
            cmd: Sequence[str] = ("oxipng", "-q", "-s", str(output_filepath))
        else:
            cmd = ("optipng", "-quiet", "-o", "1", str(output_filepath))
        logger.debug(shlex.join(cmd))
        subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=None, stderr=None, check=True
        )
    if graph_format is GraphFormat.TXT:
        # remove first 2 bytes as they cause problems with emails
        output_filepath.write_bytes(output_filepath.read_bytes()[2:])


if __name__ == "__main__":
    # parse args
    arg_parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    arg_parser.add_argument(
        "report_type",
        choices=tuple(map(str, ReportType)),
        help="Type of report",
    )
    arg_parser.add_argument("mail_from", help="Mail sender")
    arg_parser.add_argument("mail_to", help="Mail destination")
    arg_parser.add_argument(
        "-d",
        "--graph-data",
        choices=tuple(map(str, SysstatDataType)),
        default=tuple(
            str(t)
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
        "-s",
        "--img-size",
        type=int,
        nargs=2,
        default=(780, 400),
        help="Graph image size",
    )
    arg_parser.add_argument(
        "-f",
        "--img-format",
        choices=tuple(map(str, tuple(GraphFormat)[1:])),
        default=GraphFormat.PNG,
        help="Image format to use (SVG breaks rendering for some email clients)",
    )
    arg_parser.add_argument(
        "-v",
        "--verbosity",
        choices=("warning", "normal", "debug"),
        default="normal",
        help="Level of output to display",
    )
    args = arg_parser.parse_args()
    args.data_type = tuple(map(SysstatDataType, args.data_type))
    args.img_format = GraphFormat(args.img_format)

    # setup logger
    logging.basicConfig(
        level="INFO" if args.verbosity == "normal" else args.verbosity.upper(),
        format=r"%(asctime)s %(levelname)s %(message)s",
    )

    # display warning if optipng/oxipng are missing
    if (args.img_format is GraphFormat.PNG) and (not HAS_OPTIPNG) and (not HAS_OXIPNG):
        logger.warning(
            "optipng/oxipng could not be found, PNG crunching will be disabled"
        )

    # do the job
    report_type = ReportType(args.report_type)
    with tempfile.TemporaryDirectory(prefix=f"{Path(__file__).stem}_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        sysstat_data = SysstatData(report_type, temp_dir)
        minimum_files = 1 if report_type is ReportType.DAILY else 2
        if len(sysstat_data.sa_filepaths) < minimum_files:
            logger.error("Not enough data files")
            sys.exit(1)

        graph_filepaths: dict[GraphFormat, list[Path]] = {
            GraphFormat.TXT: [],
            args.img_format: [],
        }
        reboot_times = sysstat_data.get_reboot_times()

        for data_type in args.data_type:
            # data
            logger.info(f"Extracting {data_type.name} data...")
            indexes, data_filepaths = sysstat_data.generate_data_to_plot(data_type)

            # plot graph
            for graph_format in (GraphFormat.TXT, args.img_format):
                logger.info(
                    f"Generating {data_type.name} {graph_format.name} report..."
                )
                graph_filepath = temp_dir / f"{data_type}.{graph_format}"
                graph_filepaths[graph_format].append(graph_filepath)
                plot(
                    report_type,
                    graph_format,
                    args.img_size,
                    data_filepaths,
                    indexes,
                    data_type,
                    reboot_times,
                    graph_filepath,
                )

        # send mail
        logger.info("Formatting email...")
        email_data = format_email(
            args.mail_from,
            args.mail_to,
            f"Sysstat {report_type} report",
            args.img_format,
            graph_filepaths[args.img_format],
            graph_filepaths[GraphFormat.TXT],
        )

        real_mail_from = email.utils.parseaddr(args.mail_from)[1]
        real_mail_to = email.utils.parseaddr(args.mail_to)[1]
        logger.info(f"Sending email from {real_mail_from!r} to {real_mail_to!r}...")
        cmd = ("sendmail", "-f", real_mail_from, real_mail_to)
        logger.debug(shlex.join(cmd))
        subprocess.run(
            cmd, input=email_data, stdout=None, stderr=None, text=True, check=True
        )
