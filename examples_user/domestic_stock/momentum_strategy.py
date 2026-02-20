import sys
import time
import logging
import pandas as pd

sys.path.extend(['..', '.'])
import kis_auth as ka
from domestic_stock_functions import *

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 설정 구간 ---
TARGET_STOCKS = {
    "000660": 1,  # SK하이닉스 90만원
    "005380": 1,  # 현대차 50만원
    "035420": 2,  # NAVER 25만원
    "068270": 2,  # 셀트리온 25만원
    "042700": 2,  # 한미반도체 20만원
    "005930": 2,  # 삼성전자 18만원
    "086520": 2,  # 에코프로 17만원
    "042660": 3,  # 한화오션 15만원
    "272210": 3,  # 한화시스템 11만원
    "034020": 3,  # 두산에너빌리티 10만원
    "161890": 4,  # 한국콜마 7.3만원
    "015760": 5,  # 한국전력 6.3만원
    "009830": 6,  # 한화솔루션 5.3만원
    "010140": 8,  # 삼성중공업 3만원
    "032820": 20, # 우리기술 1.5만원
    "090710": 20, # 휴림로봇 1.5만원
    "085620": 20, # 미래에셋생명 1.2만원
}
BUY_THRESHOLD = 0.005  # 0.5% 급등 시 매수
SELL_THRESHOLD = -0.01  # 매수가 대비 -1% 하락 시 손절 (하한선)
TRAILING_STOP_THRESHOLD = -0.005 # 고점 대비 0.5% 하락 시 익절 (Trailing Stop)
CHECK_INTERVAL = 2    # 라이브 매매 시 체크 주기 (초)

# 모드 설정
SIMULATION_MODE = True  # True: 과거 데이터로 테스트, False: 실시간 시장 데이터로 매매
IS_REAL_MARKET = False   # True: 실전 투자 계좌, False: 모의 투자 계좌
# ----------------

# 모드에 따른 자동 설정
SVR_TYPE = "prod" if IS_REAL_MARKET else "vps"
ENV_TYPE = "real" if IS_REAL_MARKET else "demo"

def get_current_price(stock_code, sim_data=None, step=0):
    if SIMULATION_MODE and sim_data is not None:
        # 시뮬레이션: 분봉 데이터에서 현재 시점의 가격 추출
        if step < len(sim_data):
            return int(sim_data.iloc[step]['stck_prpr'])
        else:
            return None
    else:
        # 실전: API 호출로 현재가 가져오기
        df = inquire_price(env_dv=ENV_TYPE, fid_cond_mrkt_div_code="J", fid_input_iscd=stock_code)
        return int(df['stck_prpr'].iloc[0]) if not df.empty else None

def main():
    ka.auth(svr=SVR_TYPE, product="01")
    trenv = ka.getTREnv()
    
    # 각 종목별 상태 저장 (매수 여부, 매수가, 고점가 등)
    portfolio = {code: {"is_holding": False, "buy_price": 0, "high_price": 0, "history": []} for code in TARGET_STOCKS.keys()}
    
    # 시뮬레이션용 데이터 로드
    sim_data_dict = {}
    if SIMULATION_MODE:
        logger.info("시뮬레이션 모드: 오늘의 분봉 데이터를 로드합니다. (6시간)")
        for code in TARGET_STOCKS:
            all_minutes = []
            target_time = "153000" # 장 마감 시간부터 역순으로 수집
            
            for _ in range(12): # 30개씩 12번 = 6시간
                _, df_minute = inquire_time_itemchartprice(
                    env_dv="demo", fid_cond_mrkt_div_code="J", fid_input_iscd=code, 
                    fid_input_hour_1=target_time, fid_pw_data_incu_yn="Y"
                )
                if df_minute.empty: break
                
                all_minutes.append(df_minute)
                # 다음 조회를 위해 가장 과거 데이터의 시간보다 1분 전 시간 설정
                last_time = df_minute.iloc[-1]['stck_cntg_hour'] # HHMMSS
                # 단순화를 위해 시간 계산 생략하고 API의 연속 데이터 속성을 활용하거나 
                # 여기서는 가져온 데이터의 마지막 시간 직전을 타겟으로 재호출
                target_time = str(int(last_time) - 1).zfill(6)
                if int(target_time) < 90000: break # 장 시작 시간 이전이면 중단
                
                time.sleep(1) # API 과부하 방지
            
            # 수집된 데이터 합치기 및 정렬
            full_df = pd.concat(all_minutes).drop_duplicates().sort_values('stck_cntg_hour')
            sim_data_dict[code] = full_df.reset_index(drop=True)
            logger.info(f"[{code}] {len(sim_data_dict[code])}개의 분봉 데이터를 확보했습니다.")
        
        max_steps = min(len(df) for df in sim_data_dict.values())
        logger.info(f"총 {max_steps}단계 시뮬레이션을 시작합니다.")
    else:
        max_steps = 999999 # 무한 루프

    for step in range(max_steps):
        for code, qty in TARGET_STOCKS.items():
            curr_p = get_current_price(code, sim_data_dict.get(code), step)
            if curr_p is None: continue
            
            logger.info(f"[{code}] 현재가: {curr_p} (Step: {step+1}/{max_steps})")
            
            hist = portfolio[code]["history"]
            hist.append(curr_p)
            if len(hist) > 5: hist.pop(0) # 최근 5개 가격 유지
            
            # 전략 로직
            if not portfolio[code]["is_holding"]:
                # 매수 로직: 최근 5분 내 최저점 대비 BUY_THRESHOLD 이상 상승 시
                if len(hist) >= 2:
                    low_p = min(hist[:-1])
                    change = (curr_p - low_p) / low_p
                    if change >= BUY_THRESHOLD:
                        logger.info(f"[{code}] 급등 포착({change:.2%})! {qty}주 매수 주문 전송. 가격: {curr_p}")
                        # 실제 주문 (실전 투자)
                        order_cash("real", "buy", trenv.my_acct, trenv.my_prod, code, "01", str(qty), "0", "KRX")
                        portfolio[code]["is_holding"] = True
                        portfolio[code]["buy_price"] = curr_p
                        portfolio[code]["high_price"] = curr_p
            else:
                # 보유 중일 때 최고가 업데이트
                if curr_p > portfolio[code]["high_price"]:
                    portfolio[code]["high_price"] = curr_p
                    logger.info(f"[{code}] 최고가 갱신: {curr_p}")

                # 매도 로직 1: 고점 대비 하락 (Trailing Stop)
                high_p = portfolio[code]["high_price"]
                drop_from_high = (curr_p - high_p) / high_p
                
                # 매도 로직 2: 매수가 대비 하락 (Stop Loss)
                buy_p = portfolio[code]["buy_price"]
                drop_from_buy = (curr_p - buy_p) / buy_p

                if drop_from_high <= TRAILING_STOP_THRESHOLD:
                    logger.info(f"[{code}] 트레일링 스탑 발동! 고점({high_p}) 대비 하락({drop_from_high:.2%}). {qty}주 매도 주문 전송. 가격: {curr_p}")
                    order_cash("real", "sell", trenv.my_acct, trenv.my_prod, code, "01", str(qty), "0", "KRX")
                    portfolio[code]["is_holding"] = False
                elif drop_from_buy <= SELL_THRESHOLD:
                    logger.info(f"[{code}] 손절선 이탈({drop_from_buy:.2%})! {qty}주 매도 주문 전송. 가격: {curr_p}")
                    order_cash("real", "sell", trenv.my_acct, trenv.my_prod, code, "01", str(qty), "0", "KRX")
                    portfolio[code]["is_holding"] = False
        
        if not SIMULATION_MODE:
            time.sleep(CHECK_INTERVAL)
        else:
            # 시뮬레이션 시에는 로그가 너무 빠르면 보기 힘드므로 짧게 대기
            time.sleep(0.1)

    logger.info("모든 테스트/운영이 종료되었습니다.")

if __name__ == "__main__":
    main()
