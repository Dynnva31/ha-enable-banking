# Enable Banking para Home Assistant

Integración personal y de **solo lectura** que trae a Home Assistant el saldo de tus cuentas bancarias europeas mediante open banking (PSD2), usando el modo restringido gratuito de [Enable Banking](https://enablebanking.com) para tus propias cuentas.

- Un sensor de saldo por cuenta (moneda de la cuenta), con el saldo contable (`CLBD`) cuando el banco lo da.
- Porcentaje propio por cuenta (atributo `share_percent`) y tu parte en euros (`my_share`), útil para cuentas compartidas.
- Un sensor con la fecha en que caduca el permiso del banco. Cuando caduca, Home Assistant abre un aviso para reconectarlo.
- Lee cada 6 horas: PSD2 permite 4 lecturas desatendidas al día por cuenta. Guarda la última lectura en caché, así que un reinicio no gasta lecturas y un fallo puntual del banco no deja el sensor vacío.

No puede mover dinero: solo pide permiso de consulta.

## Requisitos

1. Una aplicación **Production** en el panel de Enable Banking con la dirección de vuelta `https://localhost/` y el archivo `.pem` de su clave privada.
2. Cada banco vinculado antes en el panel (**Activate by linking accounts** / **Link accounts**). En modo restringido Enable Banking solo devuelve cuentas vinculadas ahí.

## Instalación

HACS → Integraciones → menú ⋮ → Repositorios personalizados → añade este repositorio como *Integration* → instala **Enable Banking** → reinicia Home Assistant.

## Configuración

Ajustes → Dispositivos y servicios → Añadir integración → **Enable Banking**:

1. Sube el `.pem` (o pega su contenido). El ID de la aplicación se saca del nombre del archivo.
2. Elige el banco.
3. Abre el enlace, entra en el banco y acepta. Acabarás en una página de `localhost` que no carga: copia su dirección completa y pégala en Home Assistant.
4. Indica qué porcentaje de cada cuenta es tuyo.

Repite para cada banco; a partir del segundo ya no pide la clave. El porcentaje se puede cambiar luego en **Configurar**.

## Privacidad

La clave privada y los identificadores de sesión se guardan en la configuración local de Home Assistant. Los saldos solo pasan por Enable Banking (proveedor regulado por la autoridad financiera de Finlandia) y por tu Home Assistant.

Proyecto personal, sin relación con Enable Banking ni con ningún banco. Úsalo bajo tu responsabilidad y respeta las condiciones de uso del modo restringido (solo tus propias cuentas, uso no comercial).
