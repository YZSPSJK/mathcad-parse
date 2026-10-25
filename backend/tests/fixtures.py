"""合成 XML 样本，仅用于业务/解析测试，不代表 Prime 实机兼容性验证。"""
import io
import zipfile

from backend.parser import CONTENT_TYPES, INTEGRATION, ML, RELATIONSHIPS, RESULT, WS


def package(*, pending=False, result=True, matrix=False, aliases=False, formula=False, prefix="m"):
    literal = "<m:apply><m:plus/><m:real>2</m:real><m:id>b</m:id></m:apply>" if formula else "<m:real>10</m:real>"
    worksheet = f'''<w:worksheet xmlns:w="{WS}" xmlns:m="{ML}"><w:regions>
      <w:region region-id="42" left="5" top="10" width="20" height="30"><w:math><m:define><m:id labels="VARIABLE">output1</m:id>{literal}</m:define></w:math></w:region>
      <w:region region-id="900"><w:math resultRef="17"><m:eval><m:id>answer</m:id></m:eval></w:math></w:region>
    </w:regions></w:worksheet>'''
    integration = f'''<regions xmlns="{INTEGRATION}"><region region-id="42" ioTagType="Input"><inputAliases><alias>parameterAlias</alias>{"<alias>secondAlias</alias>" if aliases else ""}</inputAliases></region>
      <region region-id="900" ioTagType="Output"><outputAliases><alias>resultAlias</alias></outputAliases></region></regions>'''
    value = '<m:matrix rows="2" cols="3"><m:real>1</m:real><m:real>4</m:real><m:real>2</m:real><m:real>5</m:real><m:real>3</m:real><m:real>6</m:real></m:matrix>' if matrix else "<m:real>20</m:real>"
    results = f'<resultsList xmlns="{RESULT}" xmlns:m="{ML}"><resultData result-id="17" calculation-status="{"Pending" if pending else "Synchronized"}"><m:result>{value}</m:result></resultData></resultsList>'
    parts = {"mathcad/worksheet.xml": worksheet.replace("m:", f"{prefix}:").replace("xmlns:m=", f"xmlns:{prefix}=").encode(),
             "mathcad/integration.xml": integration.encode(),
             "[Content_Types].xml": f'<Types xmlns="{CONTENT_TYPES}"><Default Extension="xml" ContentType="application/vnd.openxmlformats-officedocument.mathprocessingml.mathcad.main+xml"/></Types>'.encode(),
             "_rels/.rels": f'<Relationships xmlns="{RELATIONSHIPS}"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="/mathcad/worksheet.xml"/></Relationships>'.encode(),
             "mathcad/media/unknown.bin": b"\x00\x01opaque"}
    if result:
        parts["mathcad/result.xml"] = results.encode()
    return archive(parts)


def archive(parts):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for path, value in parts.items():
            zip_file.writestr(path, value)
    return output.getvalue()


def formula_package():
    """A/B 数值输入、C 公式输入以及 out 复合输出，alias 刻意不同于变量名。"""
    with zipfile.ZipFile(io.BytesIO(package())) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    parts["mathcad/worksheet.xml"] = f'''<w:worksheet xmlns:w="{WS}" xmlns:m="{ML}"><w:regions>
      <w:region region-id="0"><w:math resultRef="0"><m:define><m:id labels="VARIABLE">A</m:id><m:real>12</m:real></m:define></w:math></w:region>
      <w:region region-id="1"><w:math resultRef="1"><m:define><m:id labels="VARIABLE">B</m:id><m:real>2</m:real></m:define></w:math></w:region>
      <w:region region-id="2"><w:math resultRef="2"><m:define><m:id labels="VARIABLE">C</m:id><m:apply><m:plus/><m:apply><m:plus/><m:id labels="VARIABLE">A</m:id><m:apply><m:scale/><m:real>2</m:real><m:id labels="VARIABLE">B</m:id></m:apply></m:apply><m:real>12</m:real></m:apply></m:define></w:math></w:region>
      <w:region region-id="3"><w:math resultRef="3"><m:eval><m:apply><m:plus/><m:id labels="VARIABLE">B</m:id><m:id labels="VARIABLE">C</m:id></m:apply></m:eval></w:math></w:region>
    </w:regions></w:worksheet>'''.encode()
    parts["mathcad/integration.xml"] = f'''<regions xmlns="{INTEGRATION}">
      <region region-id="0" ioTagType="Input"><inputAliases><alias>aliasA</alias></inputAliases></region>
      <region region-id="1" ioTagType="Input"><inputAliases><alias>aliasB</alias></inputAliases></region>
      <region region-id="2" ioTagType="Input"><inputAliases><alias>aliasC</alias></inputAliases></region>
      <region region-id="3" ioTagType="Output"><outputAliases><alias>out</alias></outputAliases></region>
    </regions>'''.encode()
    parts["mathcad/result.xml"] = f'''<resultsList xmlns="{RESULT}" xmlns:m="{ML}">
      <resultData result-id="0" calculation-status="Synchronized"/>
      <resultData result-id="1" calculation-status="Synchronized"/>
      <resultData result-id="2" calculation-status="Synchronized"/>
      <resultData result-id="3" calculation-status="Synchronized"><m:result><m:real>30</m:real></m:result></resultData>
    </resultsList>'''.encode()
    return archive(parts)
