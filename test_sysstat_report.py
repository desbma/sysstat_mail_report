"""Tests for sysstat_report."""

import re
import unittest
from pathlib import Path
from unittest import mock

# fake network interfaces, probed at import
with (
    mock.patch.object(Path, "iterdir", return_value=(Path("lo"), Path("eth0"))),
    mock.patch.object(Path, "read_text", return_value="1000"),
):
    import sysstat_report


class TestPlot(unittest.TestCase):
    """Tests for the plot function."""

    def test_memory_stacked(self) -> None:
        """Check memory series are disjoint and stack up to the non free memory."""
        total, free, buffers, cached, shmem, dirty = (
            sysstat_report.get_total_memory_mib() // divisor
            for divisor in (1, 8, 64, 2, 16, 128)
        )
        avail = free + cached - shmem
        # sample in KiB, as output by sysstat >= 12.7.8
        sample = {
            "timestamp": 0,
            "kbmemfree": free,
            "kbavail": avail,
            "kbmemused": total - avail,
            "kbbuffers": buffers,
            "kbcached": cached,
            "kbdirty": dirty,
        }
        sample = {name: value * 1024 for name, value in sample.items()}
        columns = list(sample)
        data_indexes = tuple(
            columns.index(name) + 1
            for name in sysstat_report.SysstatData.CSV_COLUMNS[
                sysstat_report.SysstatDataType.MEM
            ]
        )

        with mock.patch.object(sysstat_report.subprocess, "run") as run_mock:
            sysstat_report.plot(
                sysstat_report.ReportType.DAILY,
                sysstat_report.GraphFormat.SVG,
                (800, 400),
                {"": Path("mem.csv")},
                data_indexes,
                sysstat_report.SysstatDataType.MEM,
                (),
                Path("mem.svg"),
            )

        plot_line = run_mock.call_args.kwargs["input"].splitlines()[-1]
        heights = []
        for plot_cmd in plot_line.removeprefix("plot ").removesuffix(";").split(", "):
            match = re.fullmatch(
                r"'mem\.csv' using \(\$1\+-?\d+\):(.+) with filledcurve x1 title '(\w+)'",
                plot_cmd,
            )
            assert match is not None, plot_cmd
            ydata = re.sub(
                r"\$(\d+)", lambda m: str(sample[columns[int(m[1]) - 1]]), match[1]
            )
            heights.append((match[2], eval(ydata)))
        self.assertEqual(
            heights,
            [
                ("cached", total - free),
                ("dirty", total - avail + buffers + dirty),
                ("buffers", total - avail + buffers),
                ("used", total - avail),
            ],
        )


if __name__ == "__main__":
    unittest.main()
