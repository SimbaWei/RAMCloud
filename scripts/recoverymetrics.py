#!/usr/bin/env python

# Copyright (c) 2011 Stanford University
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR(S) DISCLAIM ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL AUTHORS BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

"""
This program scans the log files generated by a RAMCloud recovery,
extracts performance metrics, and print a summary of interesting data
from those metrics.
"""

from __future__ import division, print_function
from glob import glob
from optparse import OptionParser
from pprint import pprint
from functools import partial
import math
import os
import random
import re
import sys

from common import *

__all__ = ['average', 'avgAndStdDev', 'parseRecovery']

### Utilities:

class AttrDict(dict):
    """A mapping with string keys that aliases x.y syntax to x['y'] syntax.

    The attribute syntax is easier to read and type than the item syntax.
    """
    def __getattr__(self, name):
        if name not in self:
            self[name] = AttrDict()
        return self[name]
    def __setattr__(self, name, value):
        self[name] = value
    def __delattr__(self, name):
        del self[name]

    def assign(self, path, value):
        """
        Given a hierarchical path such as 'x.y.z' and
        a value, perform an assignment as if the statement
        self.x.y.z had been invoked.
        """
        names = path.split('.')
        container = self
        for name in names[0:-1]:
            if name not in container:
                container[name] = AttrDict()
            container = container[name]
        container[names[-1]] = value

def parse(f):
    """
    Scan a log file containing metrics for several servers, and return
    a list of AttrDicts, one containing the metrics for each server.
    """

    list = []
    for line in f:
        match = re.match('.* Metrics: (.*)$', line)
        if not match:
            continue
        info = match.group(1)
        start = re.match('begin server (.*)', info)
        if start:
            list.append(AttrDict())
            # Compute a human-readable name for this server (ideally
            # just its short host name).
            short_name = re.search('host=([^,]*)', start.group(1))
            if short_name:
               list[-1].server = short_name.group(1)
            else:
               list[-1].server = start.group(1)
            continue;
        if len(list) == 0:
            raise Exception, ('metrics data before "begin server" in %s'
                              % f.name)
        var, value = info.split(' ')
        list[-1].assign(var, int(value))
    if len(list) == 0:
        raise Exception, 'no metrics in %s' % f.name
    return list

def average(points):
    """Return the average of a sequence of numbers."""
    return sum(points)/len(points)

def avgAndStdDev(points):
    """Return the (average, standard deviation) of a sequence of numbers."""
    avg = average(points)
    variance = average([p**2 for p in points]) - avg**2
    if variance < 0:
        # poor floating point arithmetic made variance negative
        assert variance > -0.1
        stddev = 0.0
    else:
        stddev = math.sqrt(variance)
    return (avg, stddev)

def avgAndMax(points):
    """Return the (average, max) of a sequence of numbers."""
    if len(points) == 0:
        return (0, 0)
    max = points[0]
    for p in points:
        if p > max:
            max = p
    return (average(points), max)

def avgAndMin(points):
    """Return the (average, min) of a sequence of numbers."""
    if len(points) == 0:
        return (0, 0)
    min = points[0]
    for p in points:
        if p < min:
            min = p
    return (average(points), min)

def maxTuple(tuples):
    """Return the tuple whose first element is largest."""
    maxTuple = None;
    maxValue = 0.0;
    for tuple in tuples:
        if tuple[0] > maxValue:
            maxValue = tuple[0]
            maxTuple = tuple
    return maxTuple

def minTuple(tuples):
    """Return the tuple whose first element is smallest."""
    minTuple = None;
    minValue = 1e100;
    for tuple in tuples:
        if tuple[0] < minValue:
            minValue = tuple[0]
            minTuple = tuple
    return minTuple

def seq(x):
    """Turn the argument into a sequence.

    If x is already a sequence, do nothing. If it is not, wrap it in a list.
    """
    try:
        iter(x)
    except TypeError:
        return [x]
    else:
        return x

### Report formatting:

def defaultFormat(x):
    """Return a reasonable format string for the argument."""
    if type(x) is int:
        return '{0:6d}'
    elif type(x) is float:
        return '{0:6.1f}'
    else:
        return '{0:>6s}'

class Report(object):
    """A concatenation of Sections."""
    def __init__(self):
        self.sections = []
    def add(self, section):
        self.sections.append(section)
        return section
    def __str__(self):
        return '\n\n'.join([str(section)
                            for section in self.sections if section])

class Section(object):
    """A part of a Report consisting of lines with present metrics."""

    def __init__(self, title):
        self.title = title
        self.lines = []

    def __len__(self):
        return len(self.lines)

    def __str__(self):
        if not self.lines:
            return ''
        lines = []
        lines.append('=== {0:} ==='.format(self.title))
        maxLabelLength = max([len(label) for label, columns in self.lines])
        for label, columns in self.lines:
            lines.append('{0:<{labelWidth:}} {1:}'.format(
                '{0:}:'.format(label), columns,
                labelWidth=(maxLabelLength + 1)))
        return '\n'.join(lines)

    def line(self, label, columns, note=''):
        """Add a line of text to the Section.

        It will look like this:
            label: columns[0] / ... / columns[-1] (note)
        """
        columns = ' / '.join(columns)
        if note:
            right = '{0:s}  ({1:s})'.format(columns, note)
        else:
            right = columns
        self.lines.append((label, right))

    def avgMinSum(self, label, points, pointFormat=None, note=''):
        """Add a line with the average, minimum, and sum of a set of points.

        label and note are passed onto line()

        If more than one point is given, the columns will be the average,
        standard deviation, and sum of the points. If, however, only one point
        is given, the only column will be one showing that point.

        If pointFormat is not given, a reasonable default will be determined
        with defaultFormat(). A floating point format will be used for the
        average and standard deviation.
        """
        points = seq(points)
        columns = []
        if len(points) == 1:
            point = points[0]
            if pointFormat is None:
                pointFormat = defaultFormat(point)
            columns.append(pointFormat.format(point))
        else:
            if pointFormat is None:
                avgStdFormat = '{0:6.1f}'
                sumFormat = defaultFormat(points[0])
            else:
                avgStdFormat = pointFormat
                sumFormat = pointFormat
            avg, min = avgAndMin(points)
            columns.append('{0:} avg'.format(avgStdFormat.format(avg)))
            columns.append('min {0:}'.format(avgStdFormat.format(min)))
            columns.append('{0:} total'.format(sumFormat.format(sum(points))))
        self.line(label, columns, note)

    def avgStdFrac(self, label, points, pointFormat=None,
                 total=None, fractionLabel='', note=''):
        """Add a line with the average, std dev, and avg percentage of a
        set of points.

        label and note are passed onto line()

        If more than one point is given, the columns will be the average and
        standard deviation of the points. If total is given, an additional
        column will show the percentage of total that the average of the points
        make up.

        If, however, only one point is given, the first column will be one
        showing that point. If total is given, an additional column will show
        the percentage of total that the point makes up.

        If pointFormat is not given, a reasonable default will be determined
        with defaultFormat(). A floating point format will be used for the
        average and standard deviation.

        If total and fractionLabel are given, fractionLabel will be printed
        next to the percentage of total that points make up.
        """
        points = seq(points)
        if fractionLabel:
            fractionLabel = ' {0:}'.format(fractionLabel)
        columns = []
        if len(points) == 1:
            point = points[0]
            if pointFormat is None:
                pointFormat = defaultFormat(point)
            columns.append(pointFormat.format(point))
            if total is not None:
                columns.append('{0:6.2%}{1:}'.format(point / total,
                                                     fractionLabel))
        else:
            if pointFormat is None:
                pointFormat = '{0:6.1f}'
            avg, stddev = avgAndStdDev(points)
            columns.append('{0:} avg'.format(pointFormat.format(avg)))
            columns.append('stddev {0:}'.format(pointFormat.format(stddev)))
            if total is not None:
                columns.append('{0:6.2%} avg{1:}'.format(avg / total,
                                                         fractionLabel))
        self.line(label, columns, note)

    def avgMaxFrac(self, label, points, pointFormat=None,
                 total=None, fractionLabel='', note=''):
        """Add a line with the average, largest, and avg percentage of a
        set of points.

        label and note are passed onto line()

        If more than one point is given, the columns will be the average and
        maximum of the points. If total is given, an additional column will
        show the percentage of total that the average of the points make up.

        If, however, only one point is given, the first column will be one
        showing that point. If total is given, an additional column will show
        the percentage of total that the point makes up.

        If pointFormat is not given, a reasonable default will be determined
        with defaultFormat(). A floating point format will be used for the
        average and standard deviation.

        If total and fractionLabel are given, fractionLabel will be printed
        next to the percentage of total that points make up.
        """
        points = seq(points)
        if fractionLabel:
            fractionLabel = ' {0:}'.format(fractionLabel)
        columns = []
        if len(points) == 1:
            point = points[0]
            if pointFormat is None:
                pointFormat = defaultFormat(point)
            columns.append(pointFormat.format(point))
            if total is not None:
                columns.append('{0:6.2%}{1:}'.format(point / total,
                                                     fractionLabel))
        else:
            if pointFormat is None:
                pointFormat = '{0:6.1f}'
            avg, max = avgAndMax(points)
            columns.append('{0:} avg'.format(pointFormat.format(avg)))
            columns.append('max {0:}'.format(pointFormat.format(max)))
            if total is not None:
                columns.append('{0:6.2%} avg{1:}'.format(avg / total,
                                                         fractionLabel))
        self.line(label, columns, note)

    def avgMinFrac(self, label, points, pointFormat=None,
                 total=None, fractionLabel='', note=''):
        """Same as avgMaxFrac except print mimimum value rather than max.
        """
        points = seq(points)
        if fractionLabel:
            fractionLabel = ' {0:}'.format(fractionLabel)
        columns = []
        if len(points) == 1:
            point = points[0]
            if pointFormat is None:
                pointFormat = defaultFormat(point)
            columns.append(pointFormat.format(point))
            if total is not None:
                columns.append('{0:6.2%}{1:}'.format(point / total,
                                                     fractionLabel))
        else:
            if pointFormat is None:
                pointFormat = '{0:6.1f}'
            avg, min = avgAndMin(points)
            columns.append('{0:} avg'.format(pointFormat.format(avg)))
            columns.append('min {0:}'.format(pointFormat.format(min)))
            if total is not None:
                columns.append('{0:6.2%} avg{1:}'.format(avg / total,
                                                         fractionLabel))
        self.line(label, columns, note)

    avgStd = avgStdFrac
    """Same as avgStdFrac.

    The intent is that you don't pass total, so you won't get the Frac part.
    """

    def ms(self, label, points, **kwargs):
        """Calls avgMaxFrac to print the points shown in milliseconds.

        points and total should still be provided in full seconds!
        """
        kwargs['pointFormat'] = '{0:6.1f} ms'
        if 'total' in kwargs:
            kwargs['total'] *= 1000
        self.avgMaxFrac(label, [p * 1000 for p in seq(points)], **kwargs)

def parseRecovery(recovery_dir):
    data = AttrDict()
    data.log_dir = os.path.realpath(os.path.expanduser(recovery_dir))
    logFile = glob('%s/client.*.log' % recovery_dir)[0]

    data.backups = []
    data.masters = []
    data.servers = parse(open(logFile))
    for server in data.servers:
        # Each iteration of this loop corresponds to one server's
        # log file. Figure out whether this server is a coordinator,
        # master, backup, or both master and backup, and put the
        # data in appropriate sub-lists.
        if server.backup.recoveryCount > 0:
            data.backups.append(server)
        if server.master.recoveryCount > 0:
            data.masters.append(server)
        if server.coordinator.recoveryCount > 0:
            data.coordinator = server

    # Calculator the total number of unique server nodes (subtract 1 for the
    # coordinator).
    data.totalNodes = len(set([server.server for server in data.servers])) - 1
        
    data.client = AttrDict()
    for line in open(glob('%s/client.*.log' % recovery_dir)[0]):
        m = re.search(r'\bRecovery completed in (\d+) ns\b', line)
        if m:
            data.client.recoveryNs = int(m.group(1))
    return data

def rawSample(data):
    """Prints out some raw data for debugging"""

    print('Client:')
    pprint(data.client)
    print('Coordinator:')
    pprint(data.coordinator)
    print()
    print('Sample Master:')
    pprint(random.choice(data.masters))
    print()
    print('Sample Backup:')
    pprint(random.choice(data.backups))

def rawFull(data):
    """Prints out all raw data for debugging"""

    pprint(data)

def textReport(data):
    """Generate ASCII report"""

    coord = data.coordinator
    masters = data.masters
    backups = data.backups
    servers = data.servers

    recoveryTime = data.client.recoveryNs / 1e9
    report = Report()

    # TODO(ongaro): Size distributions of filtered segments

    summary = report.add(Section('Summary'))
    summary.avgStd('Recovery time', recoveryTime, '{0:6.3f} s')
    summary.avgStd('Masters', len(masters))
    summary.avgStd('Backups', len(backups))
    summary.avgStd('Total nodes', data.totalNodes)
    summary.avgStd('Replicas',
                   masters[0].master.replicas)
    summary.avgMaxFrac('Objects per master',
                   [master.master.liveObjectCount
                        for master in masters])
    summary.avgMaxFrac('Object size',
                   [master.master.liveObjectBytes /
                    master.master.liveObjectCount
                    for master in masters],
                   '{0:6.0f} bytes')
    summary.avgStd('Total live objects',
                   sum([master.master.liveObjectCount
                        for master in masters]))
    totalLiveObjectMB = sum([master.master.liveObjectBytes
                            for master in masters]) / 1024.0 / 1024.0
    totalRecoverySegmentMBWithOverhead = sum(
                            [master.master.segmentReadByteCount
                            for master in masters]) / 1024.0 / 1024.0
    totalrecoverySegentEntryMB = sum([master.master.recoverySegmentEntryBytes
                                     for master in masters]) / 1024.0 / 1024.0
    summary.avgStd('Total recovery segment entries',
                   sum([master.master.recoverySegmentEntryCount
                      for master in masters]))
    summary.avgStd('Total live object space', totalLiveObjectMB, '{0:6.2f} MB')
    summary.avgStd('Total recovery segment space w/ overhead',
                   totalRecoverySegmentMBWithOverhead, '{0:6.2f} MB')

    if backups:
        storageTypes = set([backup.backup.storageType for backup in backups])
        if len(storageTypes) > 1:
            storageType = 'mixed'
        else:
            storageType = {1: 'memory',
                           2: 'disk'}.get(int(storageTypes.pop()),
                                          'unknown')
        summary.line('Storage type', [storageType])
    summary.line('Log directory', [data.log_dir])

    coordSection = report.add(Section('Coordinator Time'))
    coordSection.ms('Total',
        coord.coordinator.recoveryTicks / coord.clockFrequency,
        total=recoveryTime,
        fractionLabel='of total recovery')
    coordSection.ms('  Starting recovery on backups',
        coord.coordinator.recoveryConstructorTicks / coord.clockFrequency,
        total=recoveryTime,
        fractionLabel='of total recovery')
    coordSection.ms('  Starting recovery on masters',
        coord.coordinator.recoveryStartTicks / coord.clockFrequency,
        total=recoveryTime,
        fractionLabel='of total recovery')
    coordSection.ms('  Tablets recovered',
        coord.rpc.tabletsRecoveredTicks / coord.clockFrequency,
        total=recoveryTime,
        fractionLabel='of total recovery')
    coordSection.ms('    Completing recovery on backups',
        coord.coordinator.recoveryCompleteTicks / coord.clockFrequency,
        total=recoveryTime,
        fractionLabel='of total recovery')
    coordSection.ms('  Set will',
        coord.rpc.setWillTicks / coord.clockFrequency,
        total=recoveryTime,
        fractionLabel='of total recovery')
    coordSection.ms('  Get tablet map',
        coord.rpc.getTabletMapTicks / coord.clockFrequency,
        total=recoveryTime,
        fractionLabel='of total recovery')
    coordSection.ms('  Other',
        ((coord.coordinator.recoveryTicks -
          coord.coordinator.recoveryConstructorTicks -
          coord.coordinator.recoveryStartTicks -
          coord.rpc.setWillTicks -
          coord.rpc.getTabletMapTicks -
          coord.rpc.tabletsRecoveredTicks) /
         coord.clockFrequency),
        total=recoveryTime,
        fractionLabel='of total recovery')
    coordSection.ms('Receiving in transport',
        coord.transport.receive.ticks / coord.clockFrequency,
        total=recoveryTime,
        fractionLabel='of total recovery')

    masterSection = report.add(Section('Master Time'))
    masterSection.ms('Total',
        [master.master.recoveryTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('Waiting for incoming segments',
        [master.master.segmentReadStallTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('Inside recoverSegment',
        [master.master.recoverSegmentTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('  Backup.proceed',
        [master.master.backupInRecoverTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('  Verify checksum',
        [master.master.verifyChecksumTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('  Segment append',
        [master.master.segmentAppendTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('    Segment append copy',
        [master.master.segmentAppendCopyTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('    Segment append checksum',
        [master.master.segmentAppendChecksumTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('  Other (HT, etc.)',
        [(master.master.recoverSegmentTicks -
          master.master.backupInRecoverTicks -
          master.master.verifyChecksumTicks -
          master.master.segmentAppendTicks) /
         master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery',
        note='other')
    masterSection.ms('Final log sync',
        [master.master.logSyncTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('Removing tombstones',
        [master.master.removeTombstoneTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('Other',
        [(master.master.recoveryTicks -
          master.master.segmentReadStallTicks -
          master.master.recoverSegmentTicks -
          master.master.logSyncTicks -
          master.master.removeTombstoneTicks) /
         master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('Receiving in transport',
        [master.transport.receive.ticks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('Transmitting in transport',
        [master.transport.transmit.ticks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    if (any([master.transport.sessionOpenCount for master in masters])):
        masterSection.ms('Opening sessions',
            [master.transport.sessionOpenTicks / master.clockFrequency
             for master in masters],
            total=recoveryTime,
            fractionLabel='of total recovery')
        if sum([master.transport.retrySessionOpenCount for master in masters]):
            masterSection.avgStd('  Timeouts:',
                [master.transport.retrySessionOpenCount for master in masters],
                label='!!!')
        sessionOpens = []
        for master in masters:
            if master.transport.sessionOpenCount > 0:
                avg = (master.transport.sessionOpenTicks /
                       master.transport.sessionOpenCount)
                if avg**2 > 2**64 - 1:
                    stddev = -1.0 # 64-bit arithmetic could have overflowed
                else:
                    variance = (master.transport.sessionOpenSquaredTicks /
                                master.transport.sessionOpenCount) - avg**2
                    if variance < 0:
                        # poor floating point arithmetic made variance negative
                        assert variance > -0.1
                        stddev = 0.0
                    else:
                        stddev = math.sqrt(variance)
                    stddev /= master.clockFrequency / 1e3
                avg /= master.clockFrequency / 1e3
                sessionOpens.append((avg, stddev))
            else:
                sessionOpens.append((0, 0))
        masterSection.avgMaxFrac('  Avg per session',
                             [x[0] for x in sessionOpens],
                             pointFormat='{0:6.1f} ms')
        masterSection.avgMaxFrac('  Std dev per session',
                             [x[1] for x in sessionOpens],
                             pointFormat='{0:6.1f} ms')

    masterSection.ms('Replicating one segment',
        [(master.master.replicationTicks / master.clockFrequency) /
         math.ceil(master.master.replicationBytes / master.segmentSize) /
          master.master.replicas
         for master in masters])
    try:
        masterSection.ms('  During replay',
            [((master.master.replicationTicks - master.master.logSyncTicks) /
              master.clockFrequency) /
             math.ceil((master.master.replicationBytes - master.master.logSyncBytes) /
               master.segmentSize) / master.master.replicas
             for master in masters])
    except:
        pass
    masterSection.ms('  During log sync',
        [(master.master.logSyncTicks / master.clockFrequency) /
         math.ceil(master.master.logSyncBytes / master.segmentSize) /
         master.master.replicas
         for master in masters])
    try:
        masterSection.ms('RPC latency replicating one segment',
            [(master.master.backupCloseTicks + master.master.logSyncCloseTicks) /
             master.clockFrequency /
             (master.master.backupCloseCount + master.master.logSyncCloseCount)
             for master in masters],
            note='for R-th replica')
    except:
        pass
    try:
        masterSection.ms('  During replay',
            [master.master.backupCloseTicks / master.clockFrequency /
             master.master.backupCloseCount
             for master in masters],
            note='for R-th replica')
    except:
        pass
    try:
        masterSection.ms('  During log sync',
            [master.master.logSyncCloseTicks / master.clockFrequency /
             master.master.logSyncCloseCount
             for master in masters],
            note='for R-th replica')
    except:
        pass

    masterSection.ms('Replication',
        [master.master.replicationTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('Client RPCs Active',
        [master.transport.clientRpcsActiveTicks / master.clockFrequency
         for master in masters],
        total=recoveryTime,
        fractionLabel='of total recovery')
    masterSection.ms('Average GRD completion time',
        [(master.master.segmentReadTicks / master.master.segmentReadCount)
         / master.clockFrequency
         for master in masters])

    backupSection = report.add(Section('Backup Time'))
    backupSection.ms('RPC service time',
        [backup.backup.serviceTicks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('  startReadingData',
        [backup.rpc.backupStartReadingDataTicks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('  Open/write segment',
        [backup.rpc.backupWriteTicks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('    Open segment memset',
        [backup.backup.writeClearTicks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('    Copy',
        [backup.backup.writeCopyTicks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('    Other',
        [(backup.rpc.backupWriteTicks -
          backup.backup.writeClearTicks -
          backup.backup.writeCopyTicks) / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('  getRecoveryData',
        [backup.rpc.backupGetRecoveryDataTicks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('  Other',
        [(backup.backup.serviceTicks -
          backup.rpc.backupStartReadingDataTicks -
          backup.rpc.backupWriteTicks -
          backup.rpc.backupGetRecoveryDataTicks) /
         backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('Transmitting in transport',
        [backup.transport.transmit.ticks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('Filtering segments',
        [backup.backup.filterTicks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('Reading segments',
        [backup.backup.readingDataTicks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.ms('  Using disk',
        [backup.backup.storageReadTicks / backup.clockFrequency
         for backup in backups],
        total=recoveryTime,
        fractionLabel='of total recovery')
    backupSection.avgMaxFrac('getRecoveryData completions',
        [backup.backup.readCompletionCount for backup in backups],
        '{0:.0f}')
    backupSection.avgMaxFrac('getRecoveryData retry fraction',
        [(backup.rpc.backupGetRecoveryDataCount - backup.backup.readCompletionCount)
         /backup.rpc.backupGetRecoveryDataCount
         for backup in backups
         if (backup.rpc.backupGetRecoveryDataCount > 0)], '{0:0.3f}')

    efficiencySection = report.add(Section('Efficiency'))

    # TODO(ongaro): get stddev among segments
    efficiencySection.avgStd('recoverSegment CPU',
        (sum([master.master.recoverSegmentTicks / master.clockFrequency
              for master in masters]) * 1000 /
         sum([master.master.segmentReadCount
              for master in masters])),
        pointFormat='{0:6.2f} ms avg',
        note='per filtered segment')

    # TODO(ongaro): get stddev among segments
    try:
        efficiencySection.avgStd('Writing a segment',
            (sum([backup.rpc.backupWriteTicks / backup.clockFrequency
                  for backup in backups]) * 1000 /
        # Divide count by 2 since each segment does two writes: one to open the segment
        # and one to write the data.
            sum([backup.rpc.backupWriteCount / 2
                 for backup in backups])),
            pointFormat='{0:6.2f} ms avg',
            note='backup RPC thread')
    except:
        pass

    # TODO(ongaro): get stddev among segments
    try:
        efficiencySection.avgStd('Filtering a segment',
            sum([backup.backup.filterTicks / backup.clockFrequency * 1000
                  for backup in backups]) /
            sum([backup.backup.storageReadCount
                 for backup in backups]),
            pointFormat='{0:6.2f} ms avg')
    except:
        pass
    efficiencySection.avgMinFrac('Memory bandwidth (backup copies)',
        [(backup.backup.writeCopyBytes / 2**30) /
         (backup.backup.writeCopyTicks / backup.clockFrequency)
         for backup in backups], pointFormat= '{0:6.2f} GB/s')

    networkSection = report.add(Section('Network Utilization'))
    networkSection.avgStdFrac('Aggregate',
        (sum([host.transport.transmit.byteCount
              for host in [coord] + masters + backups]) *
         8 / 2**30 / recoveryTime),
        '{0:4.2f} Gb/s',
        total=data.totalNodes*25,
        fractionLabel='of network capacity',
        note='overall')
    networkSection.avgMinSum('Master in',
        [(master.transport.receive.byteCount * 8 / 2**30) /
         recoveryTime for master in masters],
        '{0:4.2f} Gb/s',
        note='overall')
    networkSection.avgMinSum('Master out',
        [(master.transport.transmit.byteCount * 8 / 2**30) /
         recoveryTime for master in masters],
        '{0:4.2f} Gb/s',
        note='overall')
    networkSection.avgMinSum('  Master out during replication',
        [(master.master.replicationBytes * 8 / 2**30) /
          (master.master.replicationTicks / master.clockFrequency)
         for master in masters],
        '{0:4.2f} Gb/s',
        note='overall')
    networkSection.avgMinSum('  Master out during log sync',
        [(master.master.logSyncBytes * 8 / 2**30) /
         (master.master.logSyncTicks / master.clockFrequency)
         for master in masters],
        '{0:4.2f} Gb/s',
        note='overall')
    networkSection.avgMinSum('Backup in',
        [(backup.transport.receive.byteCount * 8 / 2**30) /
         recoveryTime for backup in backups],
        '{0:4.2f} Gb/s',
        note='overall')
    networkSection.avgMinSum('Backup out',
        [(backup.transport.transmit.byteCount * 8 / 2**30) /
         recoveryTime for backup in backups],
        '{0:4.2f} Gb/s',
        note='overall')

    diskSection = report.add(Section('Disk Utilization'))
    diskSection.avgMinSum('Effective bandwidth',
        [(backup.backup.storageReadBytes + backup.backup.storageWriteBytes) /
         2**20 / recoveryTime
         for backup in backups],
        '{0:6.2f} MB/s')
    try:
        diskSection.avgMinSum('Active bandwidth',
            [((backup.backup.storageReadBytes + backup.backup.storageWriteBytes) /
              2**20) /
             ((backup.backup.storageReadTicks + backup.backup.storageWriteTicks) /
              backup.clockFrequency)
             for backup in backups
             if (backup.backup.storageReadTicks +
                 backup.backup.storageWriteTicks)],
            '{0:6.2f} MB/s')
    except:
        pass
    diskSection.avgMinSum('  Reading',
        [(backup.backup.storageReadBytes / 2**20 /
            (backup.backup.storageReadTicks / backup.clockFrequency))
            for backup in backups
            if backup.backup.storageReadTicks],
        '{0:6.2f} MB/s')
    diskSection.avgMinSum('  Writing',
        [(backup.backup.storageWriteBytes / 2**20 /
            (backup.backup.storageWriteTicks / backup.clockFrequency))
            for backup in backups
            if backup.backup.storageWriteTicks],
        '{0:6.2f} MB/s')
        
    diskSection.avgMaxFrac('Disk active',
        [((backup.backup.storageReadTicks + backup.backup.storageWriteTicks) *
          100 / backup.clockFrequency) /
         recoveryTime
         for backup in backups],
        '{0:6.2f}%',
        note='of total recovery')
    diskSection.avgMaxFrac('  Reading',
        [100 * (backup.backup.storageReadTicks / backup.clockFrequency) /
         recoveryTime
         for backup in backups],
        '{0:6.2f}%',
        note='of total recovery')
    diskSection.avgMaxFrac('  Writing',
        [100 * (backup.backup.storageWriteTicks / backup.clockFrequency) /
         recoveryTime
         for backup in backups],
        '{0:6.2f}%',
        note='of total recovery')

    backupSection = report.add(Section('Backup Events'))
    backupSection.avgMaxFrac('Segments read',
        [backup.backup.storageReadCount for backup in backups])
    backupSection.avgMaxFrac('Primary segments loaded',
        [backup.backup.primaryLoadCount for backup in backups])
    backupSection.avgMaxFrac('Secondary segments loaded',
        [backup.backup.secondaryLoadCount for backup in backups])

    slowSection = report.add(Section('Slowest Servers'))
    slowest = maxTuple([
            [1e03 * (master.master.backupManagerTicks - 
             master.master.logSyncTicks) / master.clockFrequency,
             master.server] for master in masters])
    if slowest:
        slowSection.line('Backup opens, writes',
                ['{1:s} ({0:.1f} ms)'.format(*slowest)])
    slowest = maxTuple([
            [1e03 * master.master.segmentReadStallTicks /
             master.clockFrequency, master.server]
             for master in masters])
    if slowest:
        slowSection.line('Stalled reading segs from backups',
                ['{1:s} ({0:.1f} ms)'.format(*slowest)])
    slowest = minTuple([
            [(backup.backup.storageReadBytes / 2**20) / 
             (backup.backup.storageReadTicks / backup.clockFrequency),
             backup.server] for backup in backups
             if (backup.backup.storageReadTicks > 0)])
    if slowest:
        slowSection.line('Reading from disk',
                ['{1:s} ({0:.1f} MB/s)'.format(*slowest)])
    slowest = minTuple([
            [(backup.backup.storageWriteBytes / 2**20) / 
             (backup.backup.storageWriteTicks / backup.clockFrequency),
             backup.server] for backup in backups
             if backup.backup.storageWriteTicks])
    if slowest:
        slowSection.line('Writing to disk',
                ['{1:s} ({0:.1f} MB/s)'.format(*slowest)])

    tempSection = report.add(Section('Temporary Metrics'))
    for i in range(10):
        field = 'ticks{0:}'.format(i)
        points = [host.temp[field] / host.clockFrequency
                    for host in servers]
        if any(points):
            tempSection.ms('temp.%s' % (field),
                            points,
                            total=recoveryTime,
                            fractionLabel='of total recovery')
    for i in range(10):
        field = 'count{0:}'.format(i)
        points = [host.temp[field] for host in servers]
        if any(points):
            tempSection.avgMaxFrac('temp.%s' % (field),
                                points)
    return report

def main():
    ### Parse command line options
    parser = OptionParser()
    parser.add_option('-r', '--raw',
        dest='raw', action='store_true',
        help='Print out raw data (helpful for debugging)')
    parser.add_option('-a', '--all',
        dest='all', action='store_true',
        help='Print out all raw data not just a sample')
    options, args = parser.parse_args()
    if len(args) > 0:
        recovery_dir = args[0]
    else:
        recovery_dir = 'recovery/latest'

    data = parseRecovery(recovery_dir)

    if options.raw:
        if options.all:
            rawFull(data)
        else:
            rawSample(data)

    print(textReport(data))

if __name__ == '__main__':
    # x = AttrDict()
    # x.assign([["a.b.c", 2], ["d", 3], ["a.b.x", 4], ["a.z", 19]])
    # x.a.x.y = 19
    # print(x)
    sys.exit(main())

