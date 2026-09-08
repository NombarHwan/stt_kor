  ┌────────────────────────────────────────────────────────────────┐
  │ 그냥 쓰고 싶다면 (Python 불필요):                                │
  │   https://github.com/NombarHwan/stt_kor/releases/latest         │
  │   -> STT_KOR-vX.X.X-Setup.exe 를 받아 더블클릭해서 설치         │
  │ 아래 내용은 개발자용 CLI 스크립트(stt_korean.py) 사용법입니다.  │
  └────────────────────────────────────────────────────────────────┘

  사용 방법

  # 패키지 먼저 설치
  pip install faster-whisper

  # 실행
  python C:\Users\heosu\stt_korean.py 강의.mp3

  # 출력 파일 지정
  python C:\Users\heosu\stt_korean.py 강의.mp3 결과.txt

  실행하면 강의 오디오 옆에 _transcript.txt 파일이 자동 생성되고, 타임스탬프와 함께 텍스트가 저장됩니다.