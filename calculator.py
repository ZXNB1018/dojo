#!/usr/bin/env python3
"""一个简单的命令行计算器程序。"""


def calculate(a: float, operator: str, b: float) -> float:
    if operator == "+":
        return a + b
    if operator == "-":
        return a - b
    if operator == "*":
        return a * b
    if operator == "/":
        if b == 0:
            raise ZeroDivisionError("除数不能为 0")
        return a / b
    raise ValueError(f"不支持的运算符: {operator}")


def main() -> None:
    print("=== 简易计算器 ===")
    print("支持运算: +  -  *  /")
    print("输入 q 退出")

    while True:
        raw = input("请输入表达式（例如 3 + 5）: ").strip()
        if raw.lower() in {"q", "quit", "exit"}:
            print("再见！")
            break

        parts = raw.split()
        if len(parts) != 3:
            print("格式错误，请按：数字 运算符 数字")
            continue

        left_str, operator, right_str = parts
        try:
            left = float(left_str)
            right = float(right_str)
            result = calculate(left, operator, right)
            if result.is_integer():
                print(f"结果: {int(result)}")
            else:
                print(f"结果: {result}")
        except ValueError as err:
            print(f"输入错误: {err}")
        except ZeroDivisionError as err:
            print(f"运算错误: {err}")


if __name__ == "__main__":
    main()
