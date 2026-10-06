#!/usr/bin/env python3
"""KTX 취소표 감시 스크립트 (korail-mobile-api 2.3.0 기준, 코레일+ 7.0.8)

특정 열차 1편의 좌석 상태를 주기적으로 조회하다가 예약 가능해지면
'결제 전 홀드'를 만들고 알림을 보낸 뒤 종료합니다.

- 결제는 자동으로 하지 않습니다. 홀드에는 결제 기한이 있으니 기한 안에
  코레일+ 앱(내 예약)에서 직접 결제하세요.
- 홀드는 최대 1건만 만들고 즉시 종료합니다.
- 비공식 라이브러리라 코레일 서버/앱 변경 시 언제든 깨질 수 있습니다.

설치:  pip install korail-mobile-api
실행:  python ktx_watcher.py --dep 서울 --arr 부산 --date 20261010 \
           --train-no 123 --time 090000 --adults 1

로그인 정보는 환경변수 KORAIL_ID / KORAIL_PW 로 주거나, 없으면 실행 시 입력합니다.
텔레그램 알림(선택): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from getpass import getpass

from korail_mobile_api import (
    KorailClient,
    KorailPassengerCounts,
    KorailSeatClass,
    TrainSearchQuery,
)
from korail_mobile_api.errors import (
    KorailAuthError,
    KorailReservationRefusedError,
    KorailSoldOutError,
)

RELOGIN_INTERVAL_SEC = 25 * 60  # 세션 만료 대비 주기적 재로그인
MAX_CONSECUTIVE_ERRORS = 10  # 연속 오류가 이 횟수를 넘으면 종료
MAX_PAGES = 3  # 대상 열차를 찾기 위해 넘겨볼 최대 페이지 수

# '예약 가능'을 뜻하는 general/special_reservation_code 값.
# 2.4.0에서 실측 확인: '11'=좌석있음(reservation_available_flag='Y'),
# '13'=매진(flag='N'). 코레일 서버 변경 시 달라질 수 있으니 이상하면 --dry-run 으로 재확인.
RESERVABLE_CODES = {"11"}


def log(msg: str) -> None:
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(msg: str) -> None:
    """터미널 벨 + (설정 시) 텔레그램 메시지."""
    print("\a", end="", flush=True)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # 알림 실패가 본 로직을 막지 않도록
        log(f"텔레그램 알림 실패: {e}")


def login(client: KorailClient, user_id: str, password: str) -> None:
    client.login(user_id, password)
    log("로그인 완료")


def find_train(client: KorailClient, query: TrainSearchQuery, train_no: str):
    """조회 결과 페이지를 넘기며 대상 열차(train_no)를 찾는다."""
    result = client.search_trains(query)
    for _ in range(MAX_PAGES):
        for t in result.trains:
            if str(t.train_no).lstrip("0") == train_no.lstrip("0"):
                return t
        cont = result.next_page()
        if cont is None:
            break
        result = client.search_trains(query, continuation=cont)
    return None


def _general_ok(train) -> bool:
    return str(getattr(train, "general_reservation_code", "")) in RESERVABLE_CODES


def _special_ok(train) -> bool:
    return str(getattr(train, "special_reservation_code", "")) in RESERVABLE_CODES


def is_reservable(train, seat: str) -> bool:
    """RESERVABLE_CODES(상단 상수, 검증 필요) 기준으로 예약 가능 여부 판정."""
    if seat == "general":
        return _general_ok(train)
    if seat == "special":
        return _special_ok(train)
    return _general_ok(train) or _special_ok(train)  # any


def diag(train) -> str:
    """가용성 판정에 쓰이는 원시 필드를 그대로 보여준다(--dry-run / 최초 감지 시 확인용)."""
    fields = (
        "general_reservation_code", "general_reservation_flag", "general_availability_name",
        "special_reservation_code", "special_reservation_flag", "special_availability_name",
        "reservation_available_flag", "wait_reservation_flag",
    )
    parts = [f"{f}={getattr(train, f, '<없음>')!r}" for f in fields]
    return "  진단: " + " | ".join(parts)


def pick_seat_class(train, seat: str) -> KorailSeatClass:
    if seat == "special":
        return KorailSeatClass.SPECIAL
    if seat == "general":
        return KorailSeatClass.GENERAL
    # any: 일반실 우선
    if _general_ok(train):
        return KorailSeatClass.GENERAL
    return KorailSeatClass.SPECIAL


def check_history_after_failure(client: KorailClient) -> None:
    """예약 호출이 예외로 끝났을 때 홀드가 실제로 생겼는지 확인한다.

    ReservationHistoryResponse의 정확한 필드 구조는 라이브러리 버전에 따라 다를 수
    있어, 특정 필드명에 의존하지 않고 방어적으로 읽는다. 목록이 비어 있지 않으면
    '예약이 남아 있을 수 있음'으로 보고 반드시 앱에서 확인하도록 안내한다.
    """
    try:
        history = client.get_reservation_history()
    except Exception as e:
        log(f"예약 내역 확인 실패: {e!r} — 코레일+ 앱에서 직접 확인하세요.")
        notify("예약 호출 중 오류가 났고 내역 확인도 실패했습니다. 앱에서 꼭 확인하세요.")
        return

    # 내역 목록을 담고 있을 법한 속성을 순서대로 탐색 (라이브러리별 차이 흡수)
    rows = None
    for attr in ("trains", "reservations", "items", "list"):
        rows = getattr(history, attr, None)
        if rows is not None:
            break

    if not rows:
        log("남아 있는 예약이 없어 보입니다 (홀드가 생기지 않았을 가능성이 큼). "
            "그래도 앱에서 한 번 확인하세요.")
        log(f"  (원시 응답: {vars(history) if hasattr(history, '__dict__') else history!r})")
        return

    for row in rows:
        pnr = getattr(row, "pnr_no", None) or getattr(row, "pnr", "?")
        train_no = getattr(row, "train_no", "?")
        log(f"!! 계정에 예약 존재 가능: PNR={pnr} 열차={train_no} / 상세는 앱에서 확인")
    notify("예약 호출 중 오류가 났지만 계정에 예약이 남아 있을 수 있습니다. 앱에서 확인하세요.")


def main() -> int:
    ap = argparse.ArgumentParser(description="KTX 취소표 감시 → 홀드")
    ap.add_argument("--dep", required=True, help="출발역 (예: 서울)")
    ap.add_argument("--arr", required=True, help="도착역 (예: 부산)")
    ap.add_argument("--date", required=True, help="출발일 YYYYMMDD")
    ap.add_argument("--train-no", required=True, help="대상 열차 번호 (예: 123)")
    ap.add_argument("--time", default="000000",
                    help="조회 시작 시각 HHMMSS. 대상 열차 출발 시각으로 두면 1페이지에서 찾음")
    ap.add_argument("--adults", type=int, default=1, help="어른 인원수")
    ap.add_argument("--seat", choices=["general", "special", "any"], default="general")
    ap.add_argument("--interval", type=float, default=30.0,
                    help="조회 간격(초). 너무 짧게 잡으면 차단될 수 있음 (최소 15초)")
    ap.add_argument("--jitter", type=float, default=0.3,
                    help="간격에 더하는 무작위 비율 (0.3 = ±30%%)")
    ap.add_argument("--max-hours", type=float, default=12.0, help="최대 감시 시간")
    ap.add_argument("--dry-run", action="store_true",
                    help="실제 예약을 걸지 않고, 가용성 원시 필드값만 찍어서 확인(RESERVABLE_CODES 보정용)")
    args = ap.parse_args()

    if args.interval < 15:
        log("조회 간격은 최소 15초로 보정합니다.")
        args.interval = 15.0

    user_id = os.environ.get("KORAIL_ID") or input("회원번호·전화번호·이메일: ").strip()
    password = os.environ.get("KORAIL_PW") or getpass("비밀번호: ")

    query = TrainSearchQuery(
        departure_station_code=args.dep,
        arrival_station_code=args.arr,
        departure_date=args.date,
        departure_time=args.time,
        passengers=args.adults,
    )
    passengers = KorailPassengerCounts(adult=args.adults)

    client = KorailClient()
    deadline = time.monotonic() + args.max_hours * 3600
    last_login = 0.0
    errors = 0
    tries = 0

    try:
        login(client, user_id, password)
        last_login = time.monotonic()

        while time.monotonic() < deadline:
            # 세션 갱신
            if time.monotonic() - last_login > RELOGIN_INTERVAL_SEC:
                try:
                    login(client, user_id, password)
                    last_login = time.monotonic()
                except Exception as e:
                    log(f"재로그인 실패: {e!r}")

            tries += 1
            try:
                train = find_train(client, query, args.train_no)
                errors = 0
            except KorailAuthError:
                log("세션 만료 → 재로그인")
                try:
                    login(client, user_id, password)
                    last_login = time.monotonic()
                except Exception as e:
                    log(f"재로그인 실패: {e!r}")
                    errors += 1
                train = None
            except Exception as e:
                errors += 1
                log(f"조회 오류({errors}/{MAX_CONSECUTIVE_ERRORS}): {e!r}")
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    notify("KTX 감시: 연속 오류로 종료했습니다.")
                    return 2
                # 지수 백오프
                time.sleep(min(300, args.interval * (2 ** min(errors, 4))))
                continue

            if train is None:
                log(f"#{tries} 열차 {args.train_no}를 조회 결과에서 못 찾음 "
                    f"(--time/--date/--train-no 확인)")
            else:
                log(f"#{tries} {train.train_no} {train.departure_time} | "
                    f"일반: {train.general_availability_name} | "
                    f"특실: {train.special_availability_name}")

                if args.dry_run:
                    # 예약 코드값을 그대로 보여줘, RESERVABLE_CODES가 맞는지 확인하게 한다.
                    log(diag(train))
                    log(f"  → 현재 판정(RESERVABLE_CODES={RESERVABLE_CODES}): "
                        f"is_reservable={is_reservable(train, args.seat)} "
                        f"(dry-run이라 예약은 하지 않음)")
                elif is_reservable(train, args.seat):
                    log("예약 가능 감지 → 홀드 시도")
                    try:
                        hold = client.reserve(
                            train,
                            passengers=passengers,
                            seat_class=pick_seat_class(train, args.seat),
                        )
                    except KorailSoldOutError:
                        log("간발의 차로 매진 — 계속 감시합니다.")
                    except KorailReservationRefusedError as e:
                        # 중복 예약/구매 한도/예약 가능 시간 등: 재시도해도 소용없음
                        log(f"서버가 예약을 거절했습니다: {e!r}")
                        notify(f"KTX 감시: 예약 거절 — {e}")
                        return 3
                    except Exception as e:
                        # 응답을 못 읽었어도 홀드가 만들어졌을 수 있다 → 재시도 금지
                        log(f"예약 호출 오류: {e!r}")
                        check_history_after_failure(client)
                        return 4
                    else:
                        msg = (
                            f"KTX 홀드 성공! PNR={hold.pnr_no} "
                            f"금액={hold.received_amount} "
                            f"결제기한={hold.payment_deadline_date} "
                            f"{hold.payment_deadline_time}\n"
                            f"기한 안에 코레일+ 앱에서 결제하세요."
                        )
                        log(msg)
                        notify(msg)
                        return 0

            wait = args.interval * (1 + random.uniform(-args.jitter, args.jitter))
            time.sleep(max(15.0, wait))

        log("최대 감시 시간 초과, 종료합니다.")
        notify("KTX 감시: 최대 시간 초과로 종료했습니다 (표를 못 잡음).")
        return 1
    except KeyboardInterrupt:
        log("사용자 중단")
        return 130
    finally:
        try:
            client.logout()
        except Exception:
            pass
        client.close()


if __name__ == "__main__":
    sys.exit(main())
