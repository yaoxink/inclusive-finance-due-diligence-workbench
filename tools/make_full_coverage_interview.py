"""Full-coverage test interview: answers every required question and plants
off-questionnaire risk leads (asset sale / guarantee / business exit).

Reuses the SAPI TTS + ArchiveWriter seeding from make_demo_interview.
Usage: python tools/make_full_coverage_interview.py [case_id]
Synthetic data only — never real client material.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import make_demo_interview as base

LINES = [
    ('调查员', '王老师你好，我们做贷前尽调，先从家里情况问起。家里一共几口人？'),
    ('农户', '我们家一共五口人。我、我爱人、我妈，还有两个娃儿在读初中。'),
    ('农户', '我妈八十一了，腿脚不方便，平时要人照看，算一个需要照料的老人。'),
    ('调查员', '医保和养老的缴费情况说一下？'),
    ('农户', '医保家里五个人都买了。养老保险我们两口子都在缴，平均档次一年三千块。'),
    ('调查员', '你的户籍和婚姻情况是什么？'),
    ('农户', '我是邛崃本地户籍，结了婚十几年了，家庭一直比较稳定。'),
    ('调查员', '种地种了多少年？现在经营的主体是什么类型？'),
    ('农户', '我种地种了十五年了，就是自家个体农户，没得注册公司。'),
    ('调查员', '土地情况说一下？'),
    ('农户', '承包地总共有十二亩，是二轮承包的地，另外还流转了八亩地。'),
    ('农户', '流转的八亩是邻村张三家的，一亩一年八百块流转费，签了五年合同，钱是一年一付。'),
    ('调查员', '主营作物是什么？'),
    ('农户', '主营是柑橘，种了九年了，一共二十亩，亩产大概三千斤，今年收购价两块八一斤。'),
    ('农户', '按评分品类算的话，柑橘属于水果这一类。'),
    ('农户', '屋头还种了五亩油菜，一亩打三百斤菜籽，菜籽卖三块钱一斤。'),
    ('调查员', '除了种地，还有没有其他经营收入？'),
    ('农户', '农闲的时候我爱人去镇上打点零工，一年大概挣八千块。'),
    ('农户', '柑橘是跟收购商签了合同的，每年都来收，渠道比较稳定。'),
    ('调查员', '一年的成本开销大概说一下？'),
    ('农户', '种子三千，农药两千，肥料五千，水电灌溉八百，请人摘果子的人工六千。'),
    ('农户', '农具那些社会化服务费用一年一千二。'),
    ('农户', '运输储存包装这些其他支出，一年大概四千块。'),
    ('农户', '所有开销加起来，一年总成本大概两万八。'),
    ('农户', '农业保险连续买了三年了，买的是柑橘种植险，一亩保额一千二。'),
    ('调查员', '家里的房子车子情况说一下？'),
    ('农户', '县城有套商品房，自己申报价值四十万。'),
    ('农户', '车子现在就一个电动三轮车，值四千块；农机有台微耕机，买成八千块，用了三年了。'),
    ('农户', '信用社的存款还有六万块。银行贷款没得，亲戚朋友那边也不欠钱。'),
    ('农户', '不过说到钱，前年手头紧，我把那个面包车卖了，两万块钱拿来周转。'),
    ('农户', '去年我表弟贷款，我还帮他担保了五万块。'),
    ('农户', '以前还养过羊，前年亏了一笔就把羊全卖了，不养了。'),
    ('调查员', '好的，情况我记完了，后续有需要再跟你联系。'),
]


def main(case_id):
    base.LINES = LINES
    store = base.DueStore(ROOT / 'data')
    interview = store.create_interview(case_id, '全覆盖测试访谈V2·含明细与风险线索')
    print('interview:', interview['id'])
    # reuse the original seeding by monkeypatching create_interview to return ours
    original_create = store.create_interview
    store.create_interview = lambda cid, title: {'id': interview['id'], 'title': title}
    base.DueStore = lambda root: store
    base.main(case_id)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'case_db1b7b112c474cf3876b89c2871f1f2b')
