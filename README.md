# Job Application Tracker

Aplicación local para unificar el seguimiento de candidaturas desde correos, mantener evidencia verificable y publicar informes y un dashboard en la cuenta de nube elegida.

## Diseño y funciones

- Dashboard web local con tema oscuro, acentos mint/azul, filtros, indicadores, gráficos de estado y etapa, afinidad, actividad y revisión pendiente. La interfaz toma como referencia visual el panel compartido, sin depender de sus librerías remotas.
- Conector IMAP/IMAPS configurable y conector Gmail API OAuth de solo lectura.
- SQLite, clasificación conservadora, deduplicación por mensaje e historial con fecha, remitente, asunto, resumen de evidencia y enlace al mensaje Gmail cuando esté disponible.
- Afinidad de 0 a 100 solo cuando hay perfil y requisitos comparables. Los campos ausentes no se estiman. Una puntuación de 80 o más se etiqueta como prometedora; el avance se calcula por separado.
- Informes PDF y Excel dos veces al día, a las 08:00 y 18:00 de America/Bogota. El libro Excel incluye panel con tarjetas y gráficos. El dashboard HTML y los informes se pueden publicar en Google Drive o OneDrive.
- Datos, credenciales, tokens, base SQLite, informes y seed personal permanecen fuera del contenido versionado.

## Instalación en Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
job-tracker init
job-tracker setup
```

`setup` guarda la contraseña IMAP como credencial genérica en Windows Credential Manager y guarda la dirección/configuración en `%LOCALAPPDATA%\JobApplicationTracker\config.json`. Gmail usa un token OAuth local cifrado por Windows Credential Manager; la contraseña de Google no se guarda. El almacén local incluye la base SQLite, perfil, logs e informes.

### Gmail y Google Drive

En Google Cloud Console crea un proyecto, habilita Gmail API y Drive API, configura la pantalla de consentimiento y crea un cliente OAuth de tipo Desktop app. Conserva el JSON de cliente fuera de esta carpeta y configura su ruta durante `job-tracker setup`. Gmail pide `gmail.readonly`; Drive pide `drive.file` por separado. Si habilitas el envío de informes a tu propio buzón Gmail, se solicitarán `gmail.readonly` y `gmail.send`; el destinatario se obtiene del perfil autenticado y se rechaza cualquier dirección distinta. La primera autorización abre el navegador. En modo de pruebas, la pantalla de consentimiento puede mostrar la advertencia de app no verificada.

### OneDrive

Registra una aplicación pública en Microsoft Entra, habilita flujo de código de dispositivo y configura el identificador de cliente durante `job-tracker setup`. El acceso usa permisos delegados `Files.ReadWrite` y `offline_access`; la primera autorización muestra un código para iniciar sesión.

## Datos de candidaturas

El archivo `.local/conversation_seed.json` contiene únicamente registros iniciales recuperables de conversaciones. Está ignorado por Git; los estados y porcentajes desconocidos se dejan como **por verificar**. Importa datos adicionales desde un JSON local:

```powershell
job-tracker import-json C:\ruta\local\candidaturas.json
job-tracker list
```

Formato de entrada sugerido:

```json
[
  {
    "company": "Empresa",
    "role": "Cargo",
    "status": "unknown",
    "stage": "unconfirmed",
    "affinity_score": null,
    "needs_review": true,
    "source": "conversation",
    "requirements": {"required_skills": ["SQL", "Python"]}
  }
]
```

El perfil laboral se importa desde un JSON local:

```powershell
job-tracker profile C:\ruta\local\perfil.json
```

Campos admitidos: `skills`, `years_experience` y `location`. Para cada vacante, el campo `requirements` puede incluir `required_skills`, `preferred_skills`, `years_experience`, `location` y `remote`.

## Uso diario

```powershell
job-tracker sync
job-tracker report --publish
job-tracker report --email
job-tracker serve
job-tracker review
job-tracker schedule
```

`sync` consulta los mensajes y nunca edita ni elimina correo; puede enviar un informe solo si se solicita `--email`. El procesamiento conserva metadatos y resúmenes breves de evidencia; no guarda el cuerpo completo del correo en SQLite ni en la nube. Los correos sin coincidencia clara aparecen en revisión. `serve` escucha solo en `127.0.0.1`. `schedule` instala tareas a las 08:00 y 18:00 más un intento de recuperación al iniciar sesión. Con el envío automático habilitado en `setup`, cada tarea publica los informes en la nube configurada y envía el PDF y Excel al mismo buzón Gmail autenticado. La computadora debe estar encendida y conectada para una ejecución en la hora prevista.

Los informes y el dashboard cloud contienen campos estructurados de candidatura, historial y resúmenes de evidencia. Las credenciales y el contenido completo de correo no se publican. Configura la misma cuenta Google/Microsoft cuando sea la cuenta asociada al buzón; para un proveedor de correo distinto, elige por separado la cuenta de nube.

## Validación y publicación

```powershell
python -m unittest discover -s tests -v
python scripts/security_gate.py
```

El repositorio público solo contiene código y datos sintéticos de pruebas. `.gitignore` excluye `.local/`, `data/`, `reports/`, credenciales, tokens y bases SQLite. El flujo de GitHub Actions vuelve a ejecutar las pruebas sin credenciales.

En Windows, publica cada cambio lógico validado desde la raíz del repositorio:

```powershell
.\scripts\publish_change.ps1 -Message "Describe the change"
```

El script ejecuta las pruebas, agrega los archivos permitidos por `.gitignore`, analiza el árbol preparado, valida el diff, crea el commit y lo sube a `main`.

## Límites del MVP

El conector genérico requiere IMAP/IMAPS habilitado en el proveedor. Gmail API se cubre con OAuth. Un proveedor que exija un protocolo OAuth/API propietario necesita un adaptador adicional. Google Drive conserva el libro Excel y el HTML autoincluido en una carpeta privada; se pueden abrir desde Drive. OneDrive conserva el libro Excel y el HTML en la carpeta configurada.
