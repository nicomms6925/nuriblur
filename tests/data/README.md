# 회귀 세트 규약 (G0-06)

```
tests/data/
├── assets/                 # 합성용 원본 이미지 (출처·라이선스는 아래)
├── synthetic/              # scripts/make_test_clips.py 가 생성 (git 제외, 테스트가 자동 생성)
└── {genre}/{clip}.mp4      # 실제 회귀 영상 (git-lfs) + {clip}.gt.json
```

- genre 예: `cctv_street`, `cctv_parking`, `blackbox`, `interview`, `night`
- GT 형식(`{clip}.gt.json`):
  ```json
  {"fps": 30, "frames": 180, "width": 1280, "height": 720,
   "objects": [{"id": 1, "cls": "face", "boxes": {"0": [x, y, w, h], "1": [...]}}]}
  ```
  `cls ∈ {face, plate, person, vehicle}`. 다른 물체에 50% 이상 가려진 프레임은 박스를 넣지 않는다.
- 누락률 측정: `python scripts/bench.py tests/data/{genre}/{clip}.mp4 --protect-gt 1`
  (보호대상이 아닌 GT 얼굴 박스 중 마스크가 90% 미만 덮는 (프레임,얼굴) 비율)
- 실제 영상은 **비식별 처리·수집 동의를 거친 것만** 넣는다(docs/07). 원본 CCTV를 커밋하지 않는다.

## assets 출처
| 파일 | 출처 | 비고 |
|---|---|---|
| `messi5.jpg` | OpenCV `samples/data/messi5.jpg` | OpenCV 튜토리얼 샘플 이미지, 테스트 합성 전용 |
| `dog.jpg` | Megvii YOLOX `assets/dog.jpg` (Apache-2.0 저장소) | 차량(트럭) 패치 |
| `yunet_LICENSE` | OpenCV Zoo YuNet LICENSE (MIT) | 모델 라이선스 사본 |
