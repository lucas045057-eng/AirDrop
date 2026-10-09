"""Export tracked source only: deliberately exclude secrets, runtime state and dependencies."""
import argparse
import subprocess
from pathlib import Path

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('destination',type=Path);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    destination=args.destination.resolve()
    if destination.exists():raise SystemExit('目标文件已存在，请选择新文件名')
    subprocess.run(['git','archive','--format=zip','--prefix=airdrop-v2/','--output',str(destination),'HEAD'],cwd=root,check=True)
    print('已导出已提交源代码，不包含运行数据、主密钥或管理员密码。')
