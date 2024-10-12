#!/usr/bin/env python3
import sensors


def main():
    print(f"Using sensors library {sensors.VERSION} ({sensors.LIB_FILENAME})")
    print()
    sensors.init()
    try:
        for chip in sensors.iter_detected_chips():
            print(chip)
            print('Adapter:', chip.adapter_name)
            for feature in chip:
                print(
                    f"{feature.name} ({feature.label!r}):"
                    f" {feature.get_value():.1f}"
                )
                for subfeature in feature:
                    print(f"  {subfeature.name}: {subfeature.get_value():.1f}")
            print()
    finally:
        sensors.cleanup()


if __name__ == "__main__":
    main()
