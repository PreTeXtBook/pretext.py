import click
import click_logging
import json
from lxml import etree as ET
import logging
import sys
import shutil
import socket
import subprocess
import os, zipfile, requests, io
import tempfile, shutil
import git
from . import utils, static
from . import version as cli_version
from . import build as builder
from .static.pretext import pretext as core
from .project import Target,Project


log = logging.getLogger('ptxlogger')
click_logging.basic_config(log)

def raise_cli_error(message):
    raise click.UsageError(" ".join(message.split()))


#  Click command-line interface
@click.group()
# Allow a verbosity command:
@click_logging.simple_verbosity_option(log, help="Sets the severity of warnings: DEBUG for all; CRITICAL for almost none.  ERROR, WARNING, or INFO (default) are also options.")
@click.version_option(cli_version(),message=cli_version())
def main():
    """
    Command line tools for quickly creating, authoring, and building
    PreTeXt documents.
    """
    # set verbosity:
    if log.level == 10:
        verbosity = 2
    elif log.level == 50:
        verbosity = 0
    else:
        verbosity = 1
    core.set_verbosity(verbosity)
    if utils.project_path() is not None:
        log.info(f"PreTeXt project found in `{utils.project_path()}`.")
        os.chdir(utils.project_path())


# pretext new
@main.command(short_help="Generates the necessary files for a new PreTeXt project.")
@click.argument('template', default='book',
              type=click.Choice(['book', 'article'], case_sensitive=False))
@click.option('-d', '--directory', type=click.Path(), default='new-pretext-project',
              help="Directory to create/use for the project.")
@click.option('-u', '--url-template', type=click.STRING,
              help="Download a zipped template from its URL.")
def new(template,directory,url_template):
    """
    Generates the necessary files for a new PreTeXt project.
    Supports `pretext new book` (default) and `pretext new article`,
    or generating from URL with `pretext new --url-template [URL]`.
    """
    directory_fullpath = os.path.abspath(directory)
    if utils.project_path(directory_fullpath) is not None:
        log.warning(f"A project already exists in `{utils.project_path(directory_fullpath)}`.")
        log.warning(f"No new project will be generated.")
        return
    log.info(f"Generating new PreTeXt project in `{directory_fullpath}` using `{template}` template.")
    static_dir = os.path.dirname(static.__file__)
    if url_template is not None:
        r = requests.get(url_template)
        archive = zipfile.ZipFile(io.BytesIO(r.content))
    else:
        template_path = os.path.join(static_dir, 'templates', f'{template}.zip')
        archive = zipfile.ZipFile(template_path)
    # find (first) project.ptx to use as root of template
    filenames = [os.path.basename(filepath) for filepath in archive.namelist()]
    project_ptx_index = filenames.index('project.ptx')
    project_ptx_path = archive.namelist()[project_ptx_index]
    project_dir_path = os.path.dirname(project_ptx_path)
    with tempfile.TemporaryDirectory() as tmpdirname:
        for filepath in [filepath for filepath in archive.namelist() if filepath.startswith(project_dir_path)]:
            archive.extract(filepath,path=tmpdirname)
        tmpsubdirname = os.path.join(tmpdirname,project_dir_path)
        shutil.copytree(tmpsubdirname,directory,dirs_exist_ok=True)
    log.info(f"Success! Open `{directory_fullpath}/source/main.ptx` to edit your document")
    log.info(f"Then try to `pretext build` and `pretext view` from within `{directory_fullpath}`.")

# pretext init
@main.command(short_help="Generates the project manifest for a PreTeXt project in the current directory.")
def init():
    """
    Generates the project manifest for a PreTeXt project in the current directory. This feature
    is mainly intended for updating existing projects to use this CLI.
    """
    directory_fullpath = os.path.abspath('.')
    if utils.project_path(directory_fullpath) is not None:
        log.warning(f"A project already exists in `{utils.project_path(directory_fullpath)}`.")
        log.warning(f"No project manifest will be generated.")
        return
    log.info(f"Generating new PreTeXt manifest in `{directory_fullpath}`.")
    static_dir = os.path.dirname(static.__file__)
    manifest_path = os.path.join(static_dir, 'templates', 'project.ptx')
    project_ptx_path = os.path.join(directory_fullpath,"project.ptx")
    shutil.copyfile(manifest_path,project_ptx_path)
    log.info(f"Success! Open `{project_ptx_path}` to edit your manifest.")
    log.info(f"Edit your <target/>s to point to your PreTeXt source and publication files.")

# pretext build
@main.command(short_help="Build specified target")
@click.argument('target', required=False)
@click.option('-i', '--input', 'source', type=click.Path(), show_default=True,
              help='Path to main *.ptx file')
@click.option('-o', '--output', type=click.Path(), default=None, show_default=True,
              help='Path to main output directory')
@click.option('-p', '--publication', type=click.Path(), default=None, help="Publication file name, with path relative to base folder")
@click.option('--param', multiple=True, help="""
              Define a stringparam to use during processing. Usage: pretext build --param foo:bar --param baz:woo
""")
@click.option('-d', '--diagrams', is_flag=True, help='Regenerate images coded in source (latex-image, etc) using pretext script')
@click.option('-df', '--diagrams-format', default='svg', type=click.Choice(['svg', 'pdf', 'eps', 'tex'], case_sensitive=False), help="Specify output format for generated images (svg, png, etc).") # Add back in 'png' and 'all' when png works on Windows.
@click.option('-w', '--webwork', is_flag=True, default=False, help='Reprocess WeBWorK exercises, creating fresh webwork-representations.ptx file')
@click.option('-oa', '--only-assets', is_flag=True, default=False, help="Produce requested diagrams (-d) or webwork (-w) but not main build target (useful for large projects that only need to update assets")
@click.option('--pdf', is_flag=True, help='Compile LaTeX output to PDF using commandline pdflatex')
def build(target, source, output, param, publication, webwork, diagrams, diagrams_format, only_assets, pdf):
    """
    Process PreTeXt files into specified format.

    For html, images coded in source (latex-image, etc) are only processed using the --diagrams option.

    If the project included WeBWorK exercises, these must be processed using the --webwork option.
    """
    # locate manifest:
    manifest_dir = utils.project_path()
    if manifest_dir is None:
        log.warning(f"No project manifest was found.  Run `pretext init` to generate one.")
        manifest = None
        # if no target has been specified, set to old default of html.  Then set any other defaults
        if target is None:
            target = 'html'
        if source is None:
            source = 'source/main.ptx'
        if output is None:
            output = f'output/{target}'
        # set target_format to target ragardless:
        if target != 'html' and target != 'latex':
            log.critical(f'Without a project manifest, you can only build "html" or "latex".  Exiting...')
            sys.exit(f"`pretext build` did not complete.  Please try again.")
        target_format = target
    else:
        manifest = 'project.ptx'

    # Now check if no target was provided, in which case, set to first target of manifest
    if target is None:
        target = utils.project_xml().find('targets/target').get("name")
        log.info(f"Since no build target was supplied, we will build {target}, the first target of the project manifest {manifest} in {manifest_dir}")

    #if the project manifest doesn't have the target alias, exit build
    if utils.target_xml(alias=target) is None:
        log.critical("Build target does not exist in project manifest project.ptx")
        sys.exit("Exiting without completing task.")

    # Pull build info (source/output/params/etc) when not already supplied by user:
    log.debug(f"source = {source}, output = {output}, publisher = {publication}")
    if source is None:
        source = utils.target_xml(alias=target).find('source').text.strip()
        log.debug(f"No source provided, using {source}, taken from manifest")
    if output is None:
        output = utils.target_xml(alias=target).find('output-dir').text.strip()
        log.debug(f"No output provided, using {output}, taken from manifest")
    if publication is None:
        try:
            publication = utils.target_xml(alias=target).find('publication').text.strip()
            log.debug(f"No publisher file provided, using {publication}, taken from manifest")
        except:
            log.warning(f"No publisher file was found in {manifest}, will try to build anyway.")
            pass
    # TODO: get params working from manifest.

    # Set target_format to the correct thing
    try: 
        target_format = utils.target_xml(alias=target).find('format').text.strip()
        log.debug(
            f"Setting the target format to {target_format}, taken from manifest for target {target}")
    except:
        target_format = target
        log.warning(f"No format listed in the manifest for the target {target}.  Will try to build using {target} as the format.")

    # Check for xml syntax errors and quit if xml invalid:
    utils.xml_syntax_check(source)
    # Validate xml against schema; continue with warning if invalid:
    utils.schema_validate(source)
    # set up stringparams as dictionary:
    # TODO: exit gracefully if string params were not entered in correct format.
    param_list = [p.split(":") for p in param]
    stringparams = {p[0].strip(): ":".join(p[1:]).strip() for p in param_list}
    # if publication:
        # stringparams['publisher'] = publication
    if 'publisher' in stringparams:
        publication = stringparams['publisher']
    publication = os.path.abspath(publication)
    if not(os.path.isfile(publication)):
            log.error(f"You or the manifest supplied {stringparams['publisher']} as a publisher file, but it doesn't exist at that location.  Will try to build anyway.")
            static_dir = os.path.dirname(static.__file__)
            publication = os.path.join(static_dir, 'templates', 'publication.ptx')
            # raise ValueError('Publisher file ({}) does not exist'.format(stringparams['publisher']))
    # Ensure directories for assets and generated assets to avoid errors when building:
    pub_tree = ET.parse(publication)
    pub_tree.xinclude()
    element_list = pub_tree.xpath("/publication/source/directories")
    attributes_dict = element_list[0].attrib
    utils.ensure_directory(os.path.abspath(os.path.join(os.path.dirname(source), attributes_dict['external'])))
    utils.ensure_directory(os.path.abspath(os.path.join(os.path.dirname(source), attributes_dict['generated'])))
    # for key in attributes_dict:
    #     utils.ensure_directory(os.path.join(os.path.abspath(source), attributes_dict[key]))
    # set up source (input) and output as absolute paths
    source = os.path.abspath(source)
    output = os.path.abspath(output)
    #remove output directory so ptxcore doesn't complain.
    if os.path.isdir(output):
        shutil.rmtree(output)
    # put webwork-representations.ptx in same dir as source main file
    webwork_output = os.path.dirname(source)
    #build targets:
    if webwork:
        # prepare params; for now assume only server is passed
        # see documentation of pretext core webwork_to_xml
        # handle this exactly as in webwork_to_xml (should this
        # be exported in the pretext core module?)
        try:
            server_params = (stringparams['server'])
        except Exception as e:
            root_cause = str(e)
            log.warning("No server name, {}.  Using default https://webwork-ptx.aimath.org".format(root_cause))
            server_params = "https://webwork-ptx.aimath.org"
        builder.webwork(source, publication, webwork_output, stringparams, server_params)
    if diagrams:
        # TODO: read this from publisher file.
        generated_assets = 'generated-assets'
        builder.diagrams(source,publication,generated_assets,stringparams,diagrams_format)
    else:
        source_xml = ET.parse(source)
        source_xml.xinclude()
        if len(source_xml.xpath('//asymptote|//latex-image|//sageplot')) > 0 and target_format == 'html':
            log.warning("There are generated images (<latex-image/>, <asymptote/>, or <sageplot/>) or in source, but these will not be (re)built. Run pretext build with the `-d` flag if updates are needed.")
        # TODO: remove the elements that are not needed for latex.
        if len(source_xml.xpath('//asymptote|//sageplot|//video[@youtube]|//interactive[not(@preview)]')) > 0 and target_format == 'latex':
            log.warning("The source has interactive elements or videos that need a preview to be generated, but these will not be (re)built. Run `pretext build` with the `-d` flag if updates are needed.")
    if target_format=='html' and not only_assets:
        builder.html(source,publication,output,stringparams)
        # core.html(source, None, stringparams, output)
    if target_format=='latex' and not only_assets:
        builder.latex(source,publication,output,stringparams)

        # if pdf:
        #     with utils.working_directory(output):
        #         subprocess.run(['pdflatex','main.tex'])
    if target_format=='pdf' and not only_assets:
        builder.pdf(source,publication,output,stringparams)

# pretext view
@main.command(short_help="Preview built PreTeXt documents in your browser.")
@click.argument('target', required=False)
@click.option(
    '-a',
    '--access',
    type=click.Choice(['public', 'private'], case_sensitive=False),
    default='private',
    show_default=True,
    help="""
    Choose whether or not to allow other computers on your local network
    to access your documents using your IP address. (Ignored when used
    in CoCalc, which works automatically.)
    """)
@click.option(
    '-p',
    '--port',
    default=8000,
    show_default=True,
    help="""
    Choose which port to use for the local server.
    """)
@click.option(
    '-d',
    '--directory',
    type=click.Path(),
    help="""
    Serve files from provided directory
    """)
@click.option('-w', '--watch', is_flag=True, help="""
    Watch the status of project files and
    automatically rebuild target when changes
    are made. (Only supports HTML-format targets.)
    """)
def view(target,access,port,directory,watch):
    """
    Starts a local server to preview built PreTeXt documents in your browser.
    TARGET is the name of the <target/> defined in `project.ptx`.
    """
    target_name=target
    if directory is not None:
        utils.run_server(directory,access,port)
        return
    else:
        project = Project()
        target = project.target(name=target_name)
    if target is not None:
        target.view(access,port,watch)
    else:
        log.error(f"Target `{target_name}` could not be found.")

# pretext publish
@main.command(short_help="Prepares project for publishing on GitHub Pages.")
@click.argument('target', required=False)
def publish(target):
    """
    Automates HTML publication of [TARGET] on GitHub Pages to make
    the built document available to the general public.
    Requires that your project is under Git version control
    using an `origin` remote
    properly configured with GitHub and GitHub Pages. Pubilshed
    files will live in `docs` subdirectory of project.
    """
    target_name=target
    project = Project()
    target = project.target(name=target_name)
    target.publish()

## pretext debug
# @main.command(short_help="just for testing")
# def debug():
#     import os
#     from . import static, document, utils
#     log.info("This is just for debugging and testing new features.")
#     static_dir = os.path.dirname(static.__file__)
#     xslfile = os.path.join(static_dir, 'xsl', 'pretext-html.xsl')
#     print(xslfile)
