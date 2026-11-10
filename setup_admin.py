"""Display the first-start token to an operator with local container access."""
import argparse
import os
from pathlib import Path
from first_start import FirstStart, load_config
from outhman_auth import AuthService, AuthError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['token','reopen'])
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    directory=Path(os.environ.get('SYSMAN_DATA_DIR',str(root/'agents'))).resolve()
    key=Path(os.environ.get('SYSMAN_AUTH_KEY_FILE',str(root/'.sysman-secrets/outhman.key')))
    service=AuthService(directory/'auth',key,load_config(directory,key))
    try:
        manager=FirstStart(directory,key)
        if args.command=='reopen':
            backup=manager.reopen(service)
            print('Setup reopened; previous encrypted settings retained as '+backup+'. Start SysMan and retrieve a fresh setup token.')
        else: print(manager.token(service))
    except AuthError as error: parser.exit(1,str(error)+'\n')

if __name__=='__main__': main()
