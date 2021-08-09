import json
import sys
import csv
import os
import dateutil.parser

if __name__ == '__main__':
    if len(sys.argv) <= 2:
        print('error: no filename specified.')
        print('usage: %s <input file> <output file>' % sys.argv[0])
        sys.exit(1)
    if os.path.exists(sys.argv[2]):
        print('file %s already exists. overwrite? (y/n)' % (sys.argv[2],))
        answer = sys.stdin.readline().strip()
        if answer not in ['y', 'Y', 'yes', 'Yes', 'YES']:
            print('aborted.')
            sys.exit(2)
    with open(sys.argv[1], 'r') as f, open(sys.argv[2], 'w', newline="") as outf:
        linenum = 0
        csvout = csv.writer(outf)
        startTime = None
        # Write a header
        csvout.writerow(
            ['Namespace', 'Kind', 'Name', 'Reason', 'Message', 'FirstTimestamp', 'LastTimestamp', 'TimeDiff', 'Count'])
        for line in f:
            linenum = linenum + 1
            try:
                if not line.strip():
                    # Skip empty line
                    continue
                obj = json.loads(line)
                if startTime is None and obj['lastTimestamp']:
                    startTime = dateutil.parser.isoparse(obj['lastTimestamp'])
                csvout.writerow([
                    obj['involvedObject'].get('namespace', '---'),
                    obj['involvedObject'].get('kind', '---'),
                    obj['involvedObject'].get('name', '---'),
                    obj['reason'],
                    obj['message'],
                    obj['firstTimestamp'],
                    obj['lastTimestamp'],
                    str(dateutil.parser.isoparse(obj['lastTimestamp']) - startTime),
                    obj['count']
                ])
            except ValueError as e:
                print('warn: ignoring invalid json at line %d. (error=%s)' % (linenum, e))
    sys.exit(0)
