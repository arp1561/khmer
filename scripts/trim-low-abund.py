#! /usr/bin/env python
#
# This file is part of khmer, https://github.com/dib-lab/khmer/, and is
# Copyright (C) Michigan State University, 2009-2015. It is licensed under
# the three-clause BSD license; see LICENSE.
# Contact: khmer-project@idyll.org
#
"""
Trim sequences at k-mers of the given abundance, using a streaming algorithm.

Output sequences will be placed in 'infile.abundtrim'.

% python scripts/trim-low-abund.py [ <data1> [ <data2> [ ... ] ] ]

Use -h for parameter help.
"""
from __future__ import print_function
import sys
import screed
import os
import khmer
import tempfile
import shutil
import textwrap
import argparse

from screed import Record
from khmer import khmer_args

from khmer.khmer_args import (build_counting_args, info, add_loadhash_args,
                              report_on_config)
from khmer.utils import write_record, write_record_pair, broken_paired_reader
from khmer.kfile import (check_space, check_space_for_hashtable,
                         check_valid_file_exists)

DEFAULT_NORMALIZE_LIMIT = 20
DEFAULT_CUTOFF = 2


def trim_record(read, trim_at):
    new_read = Record()
    new_read.name = read.name
    new_read.sequence = read.sequence[:trim_at]
    if hasattr(read, 'quality'):
        new_read.quality = read.quality[:trim_at]

    return new_read


def get_parser():
    epilog = """
    The output is one file for each input file, <input file>.abundtrim, placed
    in the current directory.  This output contains the input sequences
    trimmed at low-abundance k-mers.

    The ``-V/--variable-coverage`` parameter will, if specified,
    prevent elimination of low-abundance reads by only trimming
    low-abundance k-mers from high-abundance reads; use this for
    non-genomic data sets that may have variable coverage.

    Note that the output reads will not necessarily be in the same order
    as the reads in the input files; if this is an important consideration,
    use ``load-into-counting.py`` and ``filter-abund.py``.  However, read
    pairs will be kept together, in "broken-paired" format; you can use
    ``extract-paired-reads.py`` to extract read pairs and orphans.

    Example::

        trim-low-abund.py -x 5e7 -k 20 -C 2 data/100k-filtered.fa
    """

    parser = build_counting_args(
        descr='Trim low-abundance k-mers using a streaming algorithm.',
        epilog=textwrap.dedent(epilog))

    parser.add_argument('input_filenames', nargs='+')

    parser.add_argument('--cutoff', '-C', type=int,
                        help='remove k-mers below this abundance',
                        default=DEFAULT_CUTOFF)

    parser.add_argument('--normalize-to', '-Z', type=int,
                        help='base cutoff on this median k-mer abundance',
                        default=DEFAULT_NORMALIZE_LIMIT)

    parser.add_argument('-o', '--out', metavar="filename",
                        type=argparse.FileType('w'),
                        default=None, help='only output a single file with '
                        'the specified filename; use a single dash "-" to '
                        'specify that output should go to STDOUT (the '
                        'terminal)')

    parser.add_argument('--variable-coverage', '-V', action='store_true',
                        default=False,
                        help='Only trim low-abundance k-mers from sequences '
                        'that have high coverage.')

    add_loadhash_args(parser)
    parser.add_argument('-s', '--savetable', metavar="filename", default='',
                        help='save the k-mer counting table to disk after all'
                        'reads are loaded.')

    # expert options
    parser.add_argument('--force', default=False, action='store_true')
    parser.add_argument('--ignore-pairs', default=False, action='store_true')
    parser.add_argument('--tempdir', '-T', type=str, default='./')

    return parser


class Trimmer(object):
    def __init__(self, graph, do_trim_low_abund, cutoff, normalize_limit):
        self.graph = graph
        self.do_trim_low_abund = do_trim_low_abund
        self.cutoff = cutoff
        self.normalize_limit = normalize_limit

        self.n_reads = 0
        self.n_bp = 0
        self.trimmed_reads = 0
        self.n_saved = 0
        self.n_skipped = 0
        self.bp_skipped = 0

    def __call__(self, reader, saver):
        graph = self.graph
        NORMALIZE_LIMIT = self.normalize_limit
        CUTOFF = self.cutoff
        K = graph.ksize()

        for n, is_pair, read1, read2 in reader:
            examine = []
            records = []

            if is_pair:
                reads = (read1, read2)
            else:
                reads = (read1,)

            # clean up the sequences for examination.
            for read in reads:
                r = read.sequence.replace('N', 'A')
                examine.append(r)

                self.n_reads += 1
                self.n_bp += len(r)

            # find out if they are estimated to have low coverage
            is_low_coverage = False
            for r in examine:
                med, _, _ = graph.get_median_count(r)
                if med < NORMALIZE_LIMIT:
                    is_low_coverage = True
                    break

            # if either of the sequences are low coverage & we have a 'saver',
            # keep both for 2nd pass
            if is_low_coverage and saver:
                for read, seq in zip(reads, examine):
                    graph.consume(seq)
                    write_record(read, saver)
                    self.n_saved += 1

            # if they're low coverage, and we don't want to trim low coverage,
            # save them.
            elif is_low_coverage and not self.do_trim_low_abund:
                for read in reads:
                    self.n_skipped += 1
                    self.bp_skipped += len(read)
                    yield read
            # otherwise, trim them if they should be trimmed, THEN write 'em
            else:
                trimmed = []
                new_records = []
                for read, seq in zip(reads, examine):
                    _, trim_at = graph.trim_on_abundance(seq, CUTOFF)
                    if trim_at >= K:
                        if trim_at != len(seq):
                            self.trimmed_reads += 1
                        read = trim_record(read, trim_at)
                    yield read


def main():
    info('trim-low-abund.py', ['streaming'])
    parser = get_parser()
    args = parser.parse_args()

    ###

    if len(set(args.input_filenames)) != len(args.input_filenames):
        print("Error: Cannot input the same filename multiple times.",
              file=sys.stderr)
        sys.exit(1)

    ###

    report_on_config(args)
    check_valid_file_exists(args.input_filenames)
    check_space(args.input_filenames, args.force)
    if args.savetable:
        check_space_for_hashtable(args, 'countgraph', args.force)

    if args.loadtable:
        print('loading countgraph from', args.loadtable, file=sys.stderr)
        ct = khmer.load_counting_hash(args.loadtable)
    else:
        print('making countgraph', file=sys.stderr)
        ct = khmer_args.create_countgraph(args)

    K = ct.ksize()
    CUTOFF = args.cutoff
    NORMALIZE_LIMIT = args.normalize_to

    tempdir = tempfile.mkdtemp('khmer', 'tmp', args.tempdir)
    print('created temporary directory %s; '
          'use -T to change location' % tempdir, file=sys.stderr)

    # ### FIRST PASS ###

    save_pass2_total = 0

    written_bp = 0
    written_reads = 0

    trimmer = Trimmer(ct, not args.variable_coverage, args.cutoff,
                      args.normalize_to)

    pass2list = []
    for filename in args.input_filenames:
        pass2filename = os.path.basename(filename) + '.pass2'
        pass2filename = os.path.join(tempdir, pass2filename)
        if args.out is None:
            trimfp = open(os.path.basename(filename) + '.abundtrim', 'w')
        else:
            trimfp = args.out

        pass2list.append((filename, pass2filename, trimfp))

        screed_iter = screed.open(filename, parse_description=False)
        pass2fp = open(pass2filename, 'w')

        n_start = trimmer.n_reads

        paired_iter = broken_paired_reader(screed_iter, min_length=K,
                                           force_single=args.ignore_pairs)

        for read in trimmer(paired_iter, pass2fp):
            write_record(read, trimfp)
            written_bp += len(read)
            written_reads += 1
        pass2fp.close()

        save_pass2 = trimmer.n_saved
        n_reads = trimmer.n_reads
        n = n_reads - n_start

#            if n % 10000 == 0:
#                print('...', n, filename, save_pass2, n_reads, n_bp,
#                      written_reads, written_bp, file=sys.stderr)


        print('%s: kept aside %d of %d from first pass, in %s' %
              (filename, save_pass2, n, filename),
              file=sys.stderr)
        save_pass2_total += save_pass2

    # ### SECOND PASS. ###

    assert trimmer.n_skipped == 0
    assert trimmer.bp_skipped == 0
    for _, pass2filename, trimfp in pass2list:
        print('second pass: looking at sequences kept aside in %s' %
              pass2filename,
              file=sys.stderr)

        # note that for this second pass, we don't care about paired
        # reads - they will be output in the same order they're read in,
        # so pairs will stay together if not orphaned.  This is in contrast
        # to the first loop.

        screed_iter = screed.open(pass2filename, parse_description=False)
        paired_iter = broken_paired_reader(screed_iter, min_length=K,
                                           force_single=True)

        for read in trimmer(paired_iter, None):
            write_record(read, trimfp)
            written_reads += 1

#            if n % 10000 == 0:
#                print('... x 2', n, pass2filename,
#                      written_reads, written_bp, file=sys.stderr)

        print('removing %s' % pass2filename, file=sys.stderr)
        os.unlink(pass2filename)

    print('removing temp directory & contents (%s)' % tempdir, file=sys.stderr)
    shutil.rmtree(tempdir)

    n_reads = trimmer.n_reads
    trimmed_reads = trimmer.trimmed_reads
    n_bp = trimmer.n_bp
    n_skipped = trimmer.n_skipped
    bp_skipped = trimmer.bp_skipped

    n_passes = 1.0 + (float(save_pass2_total) / n_reads)
    percent_reads_trimmed = float(trimmed_reads + (n_reads - written_reads)) /\
        n_reads * 100.0

    print('read %d reads, %d bp' % (n_reads, n_bp,))
    print('wrote %d reads, %d bp' % (written_reads, written_bp,))
    print('looked at %d reads twice (%.2f passes)' % (save_pass2_total,
                                                      n_passes))
    print('removed %d reads and trimmed %d reads (%.2f%%)' %
          (n_reads - written_reads, trimmed_reads, percent_reads_trimmed))
    print('trimmed or removed %.2f%% of bases (%d total)' %
          ((1 - (written_bp / float(n_bp))) * 100.0, n_bp - written_bp))

    if args.variable_coverage:
        percent_reads_hicov = 100.0 * float(n_reads - n_skipped) / n_reads
        print('%d reads were high coverage (%.2f%%);' % (n_reads - n_skipped,
                                                         percent_reads_hicov),
              file=sys.stderr)
        print('skipped %d reads/%d bases because of low coverage' %
              (n_skipped, bp_skipped),
              file=sys.stderr)

    fp_rate = \
        khmer.calc_expected_collisions(ct, args.force, max_false_pos=.8)
    # for max_false_pos see Zhang et al., http://arxiv.org/abs/1309.2975
    print('fp rate estimated to be {fpr:1.3f}'.format(fpr=fp_rate),
          file=sys.stderr)

    print('output in *.abundtrim', file=sys.stderr)

    if args.savetable:
        print("Saving k-mer counting table to",
              args.savetable, file=sys.stderr)
        ct.save(args.savetable)


if __name__ == '__main__':
    main()
