import zipfile, os, shutil, json, time, logging
from docker import Client
from werkzeug import secure_filename
from app.models import Image
from app import db
from datetime import datetime
from flask_login import current_user
from flask.templating import render_template

current_milli_time = lambda: int(round(time.time() * 1000))
DOCKER_PORT = 'unix://var/run/docker.sock'

class StreamLineBuildGenerator(object):
    def __init__(self, json_data):
        self.__dict__ = json.loads(json_data)
        
def uploadFile(ros_file, manifest_file, comments):  
    upload_path='./upload'
    logging.info('Uploading %s to path %s', ros_file.filename, upload_path)
    
          
    '''The internal unique id of a uploaded file will be the mill time since 1970''' 
    image_name = str(current_milli_time()) 
    save_filename = image_name + '.zip'
    
    '''Save file to the upload directory. Replace filename with the internal unique id'''      
    try:
        if not os.path.exists(upload_path):
            os.mkdir(upload_path)
             
        ros_file.save(os.path.join(upload_path, save_filename))
    except Exception, e:
        error_string = 'Unable to save file {} to path {}. \nReason: {}'.format(save_filename, upload_path, str(e))
        logging.error(error_string)
        return error_string
    
    logging.info('Unzipping uploaded file %s', ros_file.filename)
    
    '''Unzip the uploaded file''' 
    temp_path='./temp'
    try:
        if os.path.exists(temp_path):
            shutil.rmtree(temp_path)
        
        unzip_cmd = 'unzip ' + os.path.join(upload_path, save_filename) + ' -d ' + temp_path
        os.system(unzip_cmd)
    except Exception, e:
        error_string = 'Unzip file {} to path {} failure. \nReason: {}'.format(save_filename, temp_path, str(e))
        logging.error(error_string)
        return error_string 
    
    manifest = json.load(manifest_file)
    published_topics = manifest.get('published_topics')
    subscribed_topics = manifest.get('subscribed_topics')
    advertised_services = manifest.get('advertised_services')
    advertised_actions = manifest.get('advertised_actions')   
    start_cmds = manifest.get('start_cmds')
    if start_cmds == None :
        error_string = 'Manifest file {} does not contain start_cmds. Generate docker image failed'.format(manifest_file.getFileName())
        logging.error(error_string)
        return error_string
    rosentry = render_template('ros_entry.sh', start_cmds = start_cmds)
    with open("./temp/ros_entry.sh", "wb") as fh:
        fh.write(rosentry)
            
    '''Building the docker image''' 
    logging.info('Generating docker image with tag %s', image_name)
    try:
        docker_client = Client(base_url=DOCKER_PORT)
        generator = docker_client.build(path=".", rm = True, tag = image_name)
        
        '''Check any error by inspecting the output of build()''' 
        for line in generator:
            try:
                stream_line = StreamLineBuildGenerator(line)
                if hasattr(stream_line, "error"):
                    error_string = 'Unable to generating docker image with name {}. \nReason: {}'.format(image_name, stream_line.error)
                    logging.error(error_string)
                    return error_string
            except ValueError:
                ''' If we are not able to deserialize the received line as JSON object, just ignore it'''
                continue
    except Exception, e:
        error_string = 'Unable to generating docker image with name {}. \nReason: {}'.format(image_name, str(e))
        logging.error(error_string)
        return error_string
    
    '''Generating client-side proxy'''
    client_path='./client'
    try:
        if os.path.exists(client_path):
            shutil.rmtree(client_path)
            
        unzip_cmd = 'unzip cloudproxy.zip -d ' + client_path
        os.system(unzip_cmd)
        client_launch = render_template('client.launch', published_topics = subscribed_topics, subscribed_topics = published_topics, 
                                        advertised_services = advertised_services, url = "http://127.0.0.1:5001", image_id = image_name)
        with open("./client/cloudproxy/share/cloudproxy/launch/client.launch", "wb") as fh:
            fh.write(client_launch)
    except Exception, e:
        error_string = 'Unable to generating client proxy for image {}. \nReason: {}'.format(image_name, str(e))
        logging.error(error_string)
        return error_string
        
    shutil.rmtree(temp_path)
     
    '''Insert a new record to the image table in the database'''   
    image_record = Image(imagename = image_name, uploadname = ros_file.filename, comments = comments, uploadtime = datetime.now(), uploaduser = current_user.email)
    db.session.add(image_record)
    db.session.commit()
    
    logging.info('Uploading file %s to robotcloud successfully!', ros_file.filename)
    return None

def getContainerPort(image_name, cmd):
    logging.info('Starting a new container with image %s', image_name)
    
    try:
        '''Create container with image_name, Rosbridge port 9090 is maaped into a random port on the host machine'''
        docker_client = Client(base_url=DOCKER_PORT)
        config=docker_client.create_host_config(port_bindings={9090: None})
        container = docker_client.create_container(image=image_name, ports = [9090], host_config = config)
      
        '''Start the newly created container'''     
        container_id = container.get('Id')
        docker_client.start(container=container_id, port_bindings={9090: None})     
        
        '''Inspect state to ensure the container has been started'''
        response = docker_client.inspect_container(container_id)
        if (response == None or response.get('State') == None or not (response.get('State')).get('Running')):
            logging.error('Container %s status inspect failure. Start failed', container_id)
            return
               
    except Exception, e:
        logging.error('Unable to start the container with image %s. \nReason: %s', image_name, str(e))
        return
    
    '''Get port number on the host machine'''   
    logging.info('Finished starting a new container with id %s', container_id)
    response = docker_client.port(container_id, 9090)
    host_port = None
    if (response != None):
        host_port = response[0].get('HostPort')

    logging.info('New container is started. Websocket port on the host machine is %s.', host_port)
    return host_port