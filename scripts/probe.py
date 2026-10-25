"""只读类型库和环境探测，不启动或关闭用户的 Mathcad。"""
import json
import platform
from pathlib import Path


def probe():
    result = {"platform": platform.platform(), "verified": False}
    if platform.system() != "Windows":
        return result
    import pythoncom
    import winreg

    install = Path("C:/Program Files/PTC/Mathcad Prime 12.0.1.0")
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"MathcadPrime.Application\CLSID") as key:
        result["clsid"] = winreg.QueryValue(key, None)
    library = pythoncom.LoadTypeLib(str(install / "Ptc.MathcadPrime.Automation.tlb"))
    result["library"] = str(library.GetLibAttr())
    result["interfaces"] = {}
    for index in range(library.GetTypeInfoCount()):
        info = library.GetTypeInfo(index)
        name = info.GetDocumentation(-1)[0]
        if name in {"IMathcadPrimeWorksheet3", "IMathcadPrimeMatrix", "IMathcadPrimeSetValueResults",
                    "IMathcadPrimeInputs", "SaveOption", "IMathcadPrimeValuesSetter"}:
            attr = info.GetTypeAttr()
            result["interfaces"][name] = [
                {"names": info.GetNames(info.GetFuncDesc(n)[0]), "description": str(info.GetFuncDesc(n))}
                for n in range(attr.cFuncs)
            ]
    return result


if __name__ == "__main__":
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
