import re
import warnings
from typing import List

import pysubs2
from .formatbase import FormatBase
from .ssaevent import SSAEvent
from .ssastyle import SSAStyle
from .substation import parse_tags
from .exceptions import ContentNotUsable
from .time import ms_to_times, make_time, TIMESTAMP, timestamp_to_ms

#: Largest timestamp allowed in SubRip, ie. 99:59:59,999.
MAX_REPRESENTABLE_TIME = make_time(h=100) - 1


class SubripFormat(FormatBase):
    """SubRip Text (SRT) subtitle format implementation"""
    TIMESTAMP = TIMESTAMP

    @staticmethod
    def ms_to_timestamp(ms: int) -> str:
        """Convert ms to 'HH:MM:SS,mmm'"""
        if ms < 0:
            ms = 0
        if ms > MAX_REPRESENTABLE_TIME:
            warnings.warn("Overflow in SubRip timestamp, clamping to MAX_REPRESENTABLE_TIME", RuntimeWarning)
            ms = MAX_REPRESENTABLE_TIME
        h, m, s, ms = ms_to_times(ms)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    @staticmethod
    def timestamp_to_ms(groups):
        return timestamp_to_ms(groups)

    @classmethod
    def guess_format(cls, text):
        """See :meth:`pysubs2.formats.FormatBase.guess_format()`"""
        if b"[Script Info]" in text or b"[V4+ Styles]" in text:
            # disambiguation vs. SSA/ASS
            return None

        if text.lstrip().startswith(b"WEBVTT"):
            # disambiguation vs. WebVTT
            return None

        for line in text.splitlines():
            if len(cls.TIMESTAMP.findall(line)) == 2:
                return "srt"

    @classmethod
    def from_file(cls, subs, fp, format_, keep_html_tags=False, keep_unknown_html_tags=False, keep_newlines=False, keep_original_newlines=False, **kwargs):
        """
        See :meth:`pysubs2.formats.FormatBase.from_file()`

        Supported tags:

          - ``<i>``
          - ``<u>``
          - ``<s>``
          - ``<b>``

        Keyword args:
            keep_html_tags: If True, all HTML tags will be kept as-is instead of being
                converted to SubStation tags (eg. you will get ``<i>example</i>`` instead of ``{\\i1}example{\\i0}``).
                Setting this to True overrides the ``keep_unknown_html_tags`` option.
            keep_unknown_html_tags: If True, supported HTML tags will be converted
                to SubStation tags and any other HTML tags will be kept as-is
                (eg. you would get ``<blink>example {\\i1}text{\\i0}</blink>``).
                If False, these other HTML tags will be stripped from output
                (in the previous example, you would get only ``example {\\i1}text{\\i0}``).
        """
        timestamps = [] # (start, end)
        following_lines = [] # contains lists of lines following each timestamp

        for line in fp:
            stamps = cls.TIMESTAMP.findall(line)
            if len(stamps) == 2: # timestamp line
                start, end = map(cls.timestamp_to_ms, stamps)
                timestamps.append((start, end))
                following_lines.append([])
            else:
                if timestamps:
                    following_lines[-1].append(line)

        def prepare_text(lines):
            # Handle the "happy" empty subtitle case, which is timestamp line followed by blank line(s)
            # followed by number line and timestamp line of the next subtitle. Fixes issue #11.
            if (len(lines) >= 2
                    and all(re.match(rb"\s*$", line) for line in lines[:-1])
                    and re.match(rb"\s*\d+\s*$", lines[-1])):
                return b""

            # Handle the general case.
            s = b"".join(lines).strip()
            if not keep_original_newlines:
                # reading file to bytestring preserves the original line endings
                # convert to unix line endings
                s = re.sub(rb"\r\n", rb"\n", s)
                s = re.sub(rb"\r", rb"\n", s)
            s = re.sub(rb"\n+ *\d+ *$", b"", s) # strip number of next subtitle
            if not keep_html_tags:
                s = re.sub(rb"< *i *>", rb"{\\i1}", s)
                s = re.sub(rb"< */ *i *>", rb"{\\i0}", s)
                s = re.sub(rb"< *s *>", rb"{\\s1}", s)
                s = re.sub(rb"< */ *s *>", rb"{\\s0}", s)
                s = re.sub(rb"< *u *>", rb"{\\u1}", s)
                s = re.sub(rb"< */ *u *>", rb"{\\u0}", s)
                s = re.sub(rb"< *b *>", rb"{\\b1}", s)
                s = re.sub(rb"< */ *b *>", rb"{\\b0}", s)
            if not (keep_html_tags or keep_unknown_html_tags):
                s = re.sub(rb"< */? *[a-zA-Z][^>]*>", b"", s) # strip other HTML tags
            if not keep_newlines:
                s = re.sub(rb"\n", rb"\\N", s) # convert newlines
            return s

        subs.events = [SSAEvent(start=start, end=end, text=prepare_text(lines))
                       for (start, end), lines in zip(timestamps, following_lines)]

    @classmethod
    def to_file(cls, subs, fp, format_, apply_styles=True, keep_ssa_tags=False, **kwargs):
        """
        See :meth:`pysubs2.formats.FormatBase.to_file()`

        Italic, underline and strikeout styling is supported.

        Keyword args:
            apply_styles: If False, do not write any styling (ignore line style
                and override tags).
            keep_ssa_tags: If True, instead of trying to convert inline override
                tags to HTML (as supported by SRT), any inline tags will be passed
                to output (eg. ``{\\an7}``, which would be otherwise stripped;
                or ``{\\b1}`` instead of ``<b>``). Whitespace tags ``\\h``, ``\\n``
                and ``\\N`` will always be converted to whitespace regardless of
                this option. In the current implementation, enabling this option
                disables processing of line styles - you will get inline tags but
                if for example line's style is italic you will not get ``{\\i1}``
                at the beginning of the line. (Since this option is mostly useful
                for dealing with non-standard SRT files, ie. both input and output
                is SRT which doesn't use line styles - this shouldn't be much
                of an issue in practice.)
        """
        def prepare_text(text: bytes, style: SSAStyle):
            text = text.replace(rb"\h", b" ")
            text = text.replace(rb"\n", b"\n")
            text = text.replace(rb"\N", b"\n")

            body = []
            if keep_ssa_tags:
                body.append(text)
            else:
                for fragment, sty in parse_tags(text, style, subs.styles):
                    if apply_styles:
                        if sty.italic: fragment = b"<i>" + fragment + b"</i>"
                        if sty.underline: fragment = b"<u>" + fragment + b"</u>"
                        if sty.strikeout: fragment = b"<s>" + fragment + b"</s>"
                    if sty.drawing: raise ContentNotUsable
                    body.append(fragment)

            return re.sub(b"\n+", b"\n", b"".join(body).strip())

        visible_lines = cls._get_visible_lines(subs)

        lineno = 1
        for line in visible_lines:
            start = cls.ms_to_timestamp(line.start)
            end = cls.ms_to_timestamp(line.end)
            try:
                text = prepare_text(line.text, subs.styles.get(line.style, SSAStyle.DEFAULT_STYLE))
            except ContentNotUsable:
                continue

            print(lineno, file=fp)
            print(start, "-->", end, file=fp)
            print(text, end="\n\n", file=fp)
            lineno += 1

    @classmethod
    def _get_visible_lines(cls, subs: "pysubs2.SSAFile") -> List["pysubs2.SSAEvent"]:
        visible_lines = [line for line in subs if not line.is_comment]
        return visible_lines
