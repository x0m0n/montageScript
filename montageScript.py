"""
Usage:
montageScript [--path] [--from-picture-page] [--from-movie-page] [--no-picture-montage] [--no-movie-montage] [--testMode] [--do-not-move] [--exclude-dirs] [--recurse]
Scan --path for images and movie files to make montages.
Resume operations using respective --from switches.
"""
import argparse
import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo

TestCode = True

parser = argparse.ArgumentParser(add_help=True,allow_abbrev=True)
parser.add_argument('--path',help="Path str to where to scan. Default is '.'", default='.')
parser.add_argument('--no-picture-montage',help="Do not make montages of image files if on. Prod default False. Test default True",default=TestCode)
parser.add_argument('--no-movie-montage',help="Skip making montage of movie files if on. Prod default False. Test default True",default=TestCode)
parser.add_argument('--do-not-move',help="Do not move the files after. Prod default False. Test default True",default=TestCode)
parser.add_argument('--exclude-dirs',help="Comma deliniated str of directories to skip",default=[])
parser.add_argument('--from-picture-page',help="From which page to resume operation for picture files",default=-1)
parser.add_argument('--from-movie-page',help="From which page to resume operation for movie files",default=-1)
parser.add_argument('--recurse',help="Recursive search. Prod default True. Test default False",default=not(TestCode))


