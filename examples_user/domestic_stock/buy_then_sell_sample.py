import sys
import time
import logging

# 폴더 경로를 추가하여 kis_auth와 domestic_stock_functions를 불러올 수 있게 합니다.
sys.path.extend(['..', '.'])
import kis_auth as ka
from domestic_stock_functions import *

# 로깅 설정 (실행 과정을 보기 위함)
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 설정 구간 ---
REAL_TRADING = False  # False: 모의투자(demo), True: 실전투자(real)
# ----------------

SVR_TYPE = "prod" if REAL_TRADING else "vps"
ENV_TYPE = "real" if REAL_TRADING else "demo"

def main():
    # 1. 인증 설정
    logger.info(f"인증을 시작합니다... (모드: {'실전' if REAL_TRADING else '모의'})")
    ka.auth(svr=SVR_TYPE, product="01")
    trenv = ka.getTREnv()
    
    target_stock = "005930"  # 삼성전자
    qty = "1"                # 테스트용 1주

    # 2. 매수 주문 (시장가)
    # ord_dvsn="01"은 시장가 주문입니다. 시장가는 가격(ord_unpr)을 "0"으로 입력합니다.
    logger.info(f"[{target_stock}] {qty}주 매수 주문을 전송합니다. (시장가)")
    buy_res = order_cash(
        env_dv=ENV_TYPE, 
        ord_dv="buy", 
        cano=trenv.my_acct, 
        acnt_prdt_cd=trenv.my_prod, 
        pdno=target_stock, 
        ord_dvsn="01", 
        ord_qty=qty, 
        ord_unpr="0", 
        excg_id_dvsn_cd="KRX"
    )
    
    if not buy_res.empty:
        logger.info(f"매수 주문 성공! 주문번호: {buy_res['ODNO'].iloc[0]}")
    else:
        logger.error("매수 주문 실패")
        return

    # 3. 체결 대기 (시장가는 거의 즉시 체결되지만, 데이터 처리를 위해 잠시 대기)
    logger.info("3초간 체결을 대기합니다...")
    time.sleep(3)

    # 4. 잔고 확인 (내가 방금 산 주식이 있는지 확인)
    logger.info("현재 잔고를 확인합니다...")
    balance_df1, balance_df2 = inquire_balance(
        env_dv=ENV_TYPE, 
        cano=trenv.my_acct, 
        acnt_prdt_cd=trenv.my_prod, 
        afhr_flpr_yn="N", 
        inqr_dvsn="01", 
        unpr_dvsn="01", 
        fund_sttl_icld_yn="N", 
        fncg_amt_auto_rdpt_yn="N", 
        prcs_dvsn="00"
    )
    
    # 해당 종목이 잔고에 있는지 확인
    if balance_df1.empty:
        logger.warning("보유 중인 주식이 없습니다. (잔고가 비어 있음)")
    elif 'pdno' not in balance_df1.columns:
        logger.error(f"잔고 데이터에 'pdno' 컬럼이 없습니다. 현재 컬럼: {balance_df1.columns.tolist()}")
    else:
        stock_in_balance = balance_df1[balance_df1['pdno'] == target_stock]
        if not stock_in_balance.empty:
            holding_qty = stock_in_balance['hldg_qty'].iloc[0]
            logger.info(f"잔고 확인 완료: {target_stock}을(를) {holding_qty}주 보유 중입니다.")
        else:
            logger.warning(f"잔고에서 {target_stock}을(를) 찾을 수 없습니다. 체결이 아직 처리되지 않았을 수 있습니다.")

    # 5. 매도 주문 (시장가)
    logger.info(f"[{target_stock}] {qty}주 매도 주문을 전송합니다. (시장가)")
    sell_res = order_cash(
        env_dv=ENV_TYPE, 
        ord_dv="sell", 
        cano=trenv.my_acct, 
        acnt_prdt_cd=trenv.my_prod, 
        pdno=target_stock, 
        ord_dvsn="01", 
        ord_qty=qty, 
        ord_unpr="0", 
        excg_id_dvsn_cd="KRX"
    )
    
    if not sell_res.empty:
        logger.info(f"매도 주문 성공! 주문번호: {sell_res['ODNO'].iloc[0]}")
    else:
        logger.error("매도 주문 실패")

if __name__ == "__main__":
    main()
